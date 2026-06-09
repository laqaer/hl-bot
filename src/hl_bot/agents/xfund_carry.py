"""X-Fund Carry — market-neutral cross-sectional funding carry.

Thesis: funding is a structural, fee-independent cash flow. The coins with the
most-positive funding pay shorts every hour; the most-negative pay longs. Hold a
dollar-neutral book — SHORT the top-K highest-funding coins, LONG the bottom-K
most-negative — and collect the funding spread while staying market-neutral, so
directional moves wash out and the edge is the carry minus (maker) costs.

This is the highest-conviction candidate in the review: low directional variance,
scales cleanly with capital, and produces the kind of steady, auditable return a
capital allocator / vault depositor will fund (ROADMAP Path A+C).

Designed for maker execution (confirm with ``--prefer maker``): entries are
patient. Exit a held coin when it leaves its target set (funding normalized or
rank rotated) or its funding flips sign.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


def rolling_beta(coin_closes: list[float], mkt_closes: list[float]) -> float | None:
    """OLS beta of a coin's returns on the market's returns.

    ``beta = cov(coin_ret, mkt_ret) / var(mkt_ret)``. The two close series are
    aligned on their most-recent overlapping bars (HL gives liquid coins a bar
    every interval, so trailing windows line up). Returns ``None`` when there is
    too little data or the market has no variance (beta undefined).
    """
    n = min(len(coin_closes), len(mkt_closes))
    if n < 3:
        return None
    cc = coin_closes[-n:]
    mc = mkt_closes[-n:]
    cr = [(cc[i] - cc[i - 1]) / cc[i - 1] for i in range(1, n) if cc[i - 1]]
    mr = [(mc[i] - mc[i - 1]) / mc[i - 1] for i in range(1, n) if mc[i - 1]]
    m = min(len(cr), len(mr))
    if m < 2:
        return None
    cr, mr = cr[-m:], mr[-m:]
    mbar = sum(mr) / m
    cbar = sum(cr) / m
    var_m = sum((x - mbar) ** 2 for x in mr) / m
    if var_m <= 0:
        return None
    cov = sum((cr[i] - cbar) * (mr[i] - mbar) for i in range(m)) / m
    return cov / var_m


@dataclass
class XFundCarryConfig:
    enter_funding_per_hr: float = 0.0001       # |rate| to be eligible for a leg
    exit_funding_per_hr: float = 0.00003       # exit when |rate| falls below this
    top_k: int = 2                             # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    # When True, a held leg is NOT closed just because it rotated out of the
    # top-K rank — it is kept as long as its funding stays eligible (|rate| >=
    # exit threshold) and on the correct side. This decouples exits from rank
    # rotation to cut the cross/fee churn that buries a hold-to-collect carry.
    hold_while_eligible: bool = False
    # When True, size each leg to be *beta-neutral* (not just dollar-neutral): a
    # leg's notional is shrunk in proportion to its market beta so the book's net
    # market exposure (sum of signed beta-dollars) nets to ~0. Motivation: the
    # high-positive-funding coins we SHORT are typically higher-beta squeezing
    # alts, so a dollar-neutral book is net-short the market and the residual
    # directional variance buries the carry. Sizing is tightening-only — legs
    # only ever shrink vs the dollar-neutral baseline, never grow past the
    # per-trade cap — so it respects the risk-changes-tighten-only rule.
    beta_neutral: bool = False
    beta_market: str = "BTC"        # market proxy whose returns define beta=1
    beta_lookback: int = 48         # bars of trailing closes for the regression
    beta_floor: float = 0.3         # clamp |beta| from below (avoid huge upsizing)
    beta_cap: float = 3.0           # clamp |beta| from above
    # Minimum hours between book rebalances (0 = rebalance every tick). The carry
    # edge only survives costs at a *coarse* cadence: on real history this book is
    # net-negative at a 1h rebalance (churn pays the spread ~4x faster than carry
    # accrues) but clears the G0 gate at 4h. The live loop ticks every ~5 min, so
    # without this gate a deployed xfund book would rotate every tick and bleed.
    # When >0, within the cooldown window the agent makes no NEW entries and skips
    # rank-rotation exits, but STILL takes risk-reducing exits (funding flipped to
    # the wrong side / normalized) so de-risking is never delayed.
    rebalance_hours: float = 0.0


class XFundCarryAgent(Agent):
    def __init__(
        self,
        name: str = "xfund_carry_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = XFundCarryConfig(
            enter_funding_per_hr=float(c.get("enter_funding_per_hr", 0.0001)),
            exit_funding_per_hr=float(c.get("exit_funding_per_hr", 0.00003)),
            top_k=int(c.get("top_k", 2)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
            hold_while_eligible=bool(c.get("hold_while_eligible", False)),
            beta_neutral=bool(c.get("beta_neutral", False)),
            beta_market=str(c.get("beta_market", "BTC")),
            beta_lookback=int(c.get("beta_lookback", 48)),
            beta_floor=float(c.get("beta_floor", 0.3)),
            beta_cap=float(c.get("beta_cap", 3.0)),
            rebalance_hours=float(c.get("rebalance_hours", 0.0)),
        )
        self.conn = conn

    def _funding_side(self, f: float) -> str:
        """Side a carry leg should hold for funding ``f``: short (+f) collects,
        long (−f) collects."""
        return "A" if f > 0 else "B"

    def _beta_scales(self, coins: list[str], view: MarketView) -> dict[str, float]:
        """Per-leg notional multiplier (in ``(0, 1]``) for beta-neutral sizing.

        Returns ``{}`` (→ caller uses 1.0, i.e. dollar-neutral) when disabled or
        when the market proxy / betas can't be computed. Otherwise each leg is
        scaled by ``ref / clamp(|beta|)`` where ``ref`` is the smallest clamped
        beta in the book, so every leg carries the same beta-dollars (book
        beta-neutral) and the largest scale is exactly 1.0 (tightening-only).
        """
        if not self.cfg.beta_neutral:
            return {}
        closes = view.extra.get("closes", {}) or {}
        win = self.cfg.beta_lookback + 1  # +1 close → beta_lookback returns
        mkt = (closes.get(self.cfg.beta_market) or [])[-win:]
        if len(mkt) < 3:
            return {}
        clamped: dict[str, float] = {}
        for c in coins:
            b = rolling_beta((closes.get(c) or [])[-win:], mkt)
            if b is None:
                continue
            clamped[c] = min(max(abs(b), self.cfg.beta_floor), self.cfg.beta_cap)
        if len(clamped) < 2:
            return {}
        ref = min(clamped.values())
        return {c: ref / b for c, b in clamped.items()}

    def _open_positions(self) -> dict[str, dict]:
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px
               FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
               ORDER BY ts_ms ASC""",
            (self.name,),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {"side": r["side"], "sz": float(r["sz"] or 0),
                                      "entry_px": float(r["px"] or 0), "ts_ms": r["ts_ms"]}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def _last_rebalance_ts(self) -> int | None:
        """Timestamp (ms) of this agent's most recent book change, or None.

        Counts both entries and exits so the rebalance clock advances on any book
        change, even a tick that only flattened legs."""
        if self.conn is None:
            return None
        row = self.conn.execute(
            """SELECT MAX(ts_ms) AS t FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')""",
            (self.name,),
        ).fetchone()
        t = row["t"] if row else None
        return int(t) if t is not None else None

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        funding = view.funding or {}
        # The enter/exit thresholds and APR display are PER-HOUR, but the backtest
        # data layer scales Frame.funding by the bar length (4h bar → 4× the hourly
        # rate). Normalize back to a per-hour rate so one config behaves identically
        # across bar intervals. Live HL funding is already hourly (bar_hours=1), so
        # this is a no-op live. Without it, a 4h backtest silently runs a 4×-looser
        # entry filter and overstates the carry edge.
        bar_hours = float(view.extra.get("bar_hours", 1.0) or 1.0)
        if bar_hours != 1.0:
            funding = {c: f / bar_hours for c, f in funding.items()}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # Cadence gate: within the rebalance cooldown, hold the book steady (no
        # new entries, no rank-rotation churn) but still allow risk-reducing exits.
        within_cooldown = False
        if self.cfg.rebalance_hours > 0:
            last = self._last_rebalance_ts()
            if last is not None:
                now_ms = int(time.time() * 1000)
                within_cooldown = (now_ms - last) < self.cfg.rebalance_hours * 3_600_000

        eligible = [
            (c, f) for c, f in funding.items()
            if vol.get(c, 0) >= self.cfg.min_daily_volume_usd and (view.mids.get(c) or 0) > 0
        ]
        ranked = sorted(eligible, key=lambda kv: kv[1])
        longs = [c for c, f in ranked if f <= -self.cfg.enter_funding_per_hr][: self.cfg.top_k]
        shorts = [c for c, f in reversed(ranked) if f >= self.cfg.enter_funding_per_hr][: self.cfg.top_k]
        desired: dict[str, str] = {c: "B" for c in longs}
        desired.update({c: "A" for c in shorts})

        # ---- exits: leave the book when a coin drops out of the target set ----
        for coin, pos in list(open_pos.items()):
            f = funding.get(coin)
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if f is not None and abs(f) < self.cfg.exit_funding_per_hr:
                reason = f"FUNDING-NORMALIZED ({f*100:+.4f}%/hr)"
            elif f is not None and self._funding_side(f) != pos["side"]:
                reason = "FUNDING FLIPPED — wrong side now"
            elif want is None and not self.cfg.hold_while_eligible and not within_cooldown:
                reason = "DROPPED from carry set (rank rotated / funding eased)"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"XFUND EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "funding": f},
                ))

        # ---- entries: fill empty target slots, dollar-neutral per leg ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional
        beta_scales = self._beta_scales(list(desired), view)

        for coin, side in desired.items():
            if within_cooldown or room <= 0 or room_notional < 5.0:
                break
            if coin in active_after:
                continue
            mid = view.mids.get(coin)
            f = funding.get(coin)
            if not mid or f is None:
                continue
            scale = beta_scales.get(coin, 1.0)
            notional = min(self.cfg.max_notional_per_trade * scale, room_notional)
            if notional < 5.0:
                continue  # beta-scaled too small to be worth the min ticket; try next leg
            sz = round(notional / mid, 5)
            direction = "short" if side == "A" else "long"
            apr = (f or 0) * 8760 * 100
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side, sz=sz, px=mid,
                cloid=make_cloid(self.name),
                reasoning=(
                    f"XFUND ENTER {direction} {coin} @ ${mid:.4f} "
                    f"funding {f*100:+.4f}%/hr ({apr:+.0f}% APR), notional ${notional:.2f}"
                ),
                market_snapshot={"funding": f, "mid": mid, "annualized_pct": apr,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(f"no carry: {len(longs)} long / {len(shorts)} short legs "
                           f"above {self.cfg.enter_funding_per_hr*100:.4f}%/hr"),
                market_snapshot={"n_longs": len(longs), "n_shorts": len(shorts)},
            ))
        return out
