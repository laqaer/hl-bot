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
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class XFundCarryConfig:
    enter_funding_per_hr: float = 0.0001       # |rate| to be eligible for a leg
    exit_funding_per_hr: float = 0.00003       # exit when |rate| falls below this
    top_k: int = 2                             # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6


class XFundCarryAgent(Agent):
    default_execution = "maker"  # patient carry entries must not pay the spread

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
        )
        self.conn = conn

    def _open_positions(self) -> dict[str, dict]:
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px
               FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
                 AND is_paper = ?
               ORDER BY ts_ms ASC""",
            (self.name, 0 if self.is_live else 1),
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

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        funding = view.funding or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

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
            elif want is None:
                reason = "DROPPED from carry set (rank rotated / funding eased)"
            elif want != pos["side"]:
                reason = "FUNDING FLIPPED — wrong side now"
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

        for coin, side in desired.items():
            if room <= 0 or room_notional < 5.0:
                break
            if coin in active_after:
                continue
            mid = view.mids.get(coin)
            f = funding.get(coin)
            if not mid or f is None:
                continue
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
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
