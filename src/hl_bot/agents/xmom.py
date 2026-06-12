"""X-Mom — market-neutral cross-sectional momentum (B-EDGE3).

Thesis: relative strength persists at multi-day horizons — coins that
outperformed the cross-section over the past ~week keep outperforming the
laggards for a while. Hold a dollar-neutral book — LONG the top-K
trailing-return coins, SHORT the bottom-K — so the market component washes
out and what is left is the relative-momentum spread, net of costs.

This is a different machine from breakout (time-series momentum: each coin
judged against its own history, book net-directional in trends): xmom ranks
coins against each other and is ~market-neutral by construction. The carry
pruning lesson (Iters 20–23: cross-sectional *funding* ranking — price
variance buries a tiny fixed cash flow) does not transfer directly, because
here the signal IS the price action, not a small carry.

Entry : rank eligible coins by trailing return over ``lookback_bars``,
        optionally skipping the most-recent ``skip_bars`` (the classic guard
        against short-term reversal contaminating the momentum signal — the
        reversal at <1d horizons is exactly what twap_mr harvests). Long the
        top ``top_k``, short the bottom ``top_k``. Entries require a cross-
        section of at least 2×top_k ranked coins, else ranks are noise.
        ``invert`` flips the signal sign — long the biggest losers, short the
        biggest winners (cross-sectional short-term reversal; the momentum
        promotion case died on extended history, Iter 74). Ranking, the
        ``min_abs_return`` floor, and hysteresis all judge the signed signal,
        so under invert they operate on |drawup/drawdown| symmetrically.
Exit  : rank hysteresis — a long leaves only when it falls out of the top
        ``exit_rank`` ranks (a short, the bottom ones); plus a safety stop
        and max-hold. Hysteresis instead of exact-rank-rotation exits because
        xfund showed rotation churn eats a cross-sectional book (Iter 23).
Data  : ``view.extra[closes_key]`` trailing closes per coin, current bar
        last. Lookback is in BARS — at 1h bars 168 = 7 days.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


def trailing_return(
    closes: list[float], lookback: int, skip: int = 0
) -> float | None:
    """Return over the ``lookback`` bars ending ``skip`` bars ago.

    ``closes`` is trailing history, current bar last. With ``skip=0`` this is
    the plain lookback return; ``skip>0`` drops the most-recent bars from the
    signal so yesterday's bounce doesn't pollute last week's trend. Returns
    None when there isn't enough history or the base price is degenerate.
    """
    if lookback < 1 or skip < 0 or len(closes) < lookback + skip + 1:
        return None
    end = closes[-1 - skip]
    base = closes[-1 - skip - lookback]
    if base <= 0 or end <= 0:
        return None
    return end / base - 1.0


@dataclass
class XMomConfig:
    lookback_bars: int = 168          # trailing-return window (bars; 168×1h = 7d)
    skip_bars: int = 0                # most-recent bars excluded from the signal
    invert: bool = False              # reversal: long losers / short winners
    top_k: int = 2                    # legs per side
    exit_rank: int = 5                # hysteresis: exit when out of the top/bottom N
    min_abs_return: float = 0.0       # |trailing return| floor to enter a leg
    min_daily_volume_usd: float = 10_000_000.0
    stop_loss_pct: float = 0.05
    max_hold_hours: float = 168.0
    reentry_cooldown_hours: float = 4.0   # no fresh entry right after an exit
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    closes_key: str = "closes"        # view.extra key carrying trailing closes


class XMomAgent(Agent):
    def __init__(
        self,
        name: str = "xmom_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = XMomConfig(
            lookback_bars=int(c.get("lookback_bars", 168)),
            skip_bars=int(c.get("skip_bars", 0)),
            invert=bool(c.get("invert", False)),
            top_k=int(c.get("top_k", 2)),
            exit_rank=int(c.get("exit_rank", 5)),
            min_abs_return=float(c.get("min_abs_return", 0.0)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.05)),
            max_hold_hours=float(c.get("max_hold_hours", 168.0)),
            reentry_cooldown_hours=float(c.get("reentry_cooldown_hours", 4.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
            closes_key=str(c.get("closes_key", "closes")),
        )
        self.conn = conn

    def _position_state(self) -> tuple[dict[str, dict], dict[str, int]]:
        """Replay this agent's decision log → (open positions, last flatten ts).

        Same audit-log replay as breakout/xfund; replays only the book matching
        the current tick mode (``paper_book``).
        """
        if self.conn is None:
            return {}, {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px
               FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
                 AND is_paper=?
               ORDER BY ts_ms ASC""",
            (self.name, 1 if self.paper_book else 0),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        last_flat_ms: dict[str, int] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {
                    "ts_ms": r["ts_ms"], "side": r["side"],
                    "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0),
                }
            else:
                open_by_coin.pop(coin, None)
                last_flat_ms[coin] = r["ts_ms"]
        return open_by_coin, last_flat_ms

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        cfg = self.cfg
        closes_by_coin: dict[str, list[float]] = (
            view.extra.get(cfg.closes_key, {}) or {}
        )
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos, last_flat_ms = self._position_state()
        now_ms = int(time.time() * 1000)

        # rets holds the SIGNED SIGNAL: the raw trailing return for momentum,
        # its negation under invert (reversal). Everything downstream — ranks,
        # the min_abs_return floor, hysteresis — judges the signal; raw
        # returns are recovered (sign * signal) only for the audit trail.
        sign = -1.0 if cfg.invert else 1.0
        rets: dict[str, float] = {}
        for coin, closes in closes_by_coin.items():
            if vol.get(coin, 0) < cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            r = trailing_return(closes, cfg.lookback_bars, cfg.skip_bars)
            if r is not None:
                rets[coin] = sign * r
        ranked = sorted(rets.items(), key=lambda kv: kv[1], reverse=True)
        # 1-based rank from each end: rank_top[c]=1 is the strongest coin,
        # rank_bot[c]=1 the weakest. Hysteresis exits judge these.
        rank_top = {c: i + 1 for i, (c, _) in enumerate(ranked)}
        rank_bot = {c: len(ranked) - i for i, (c, _) in enumerate(ranked)}

        longs: list[str] = []
        shorts: list[str] = []
        if len(ranked) >= 2 * cfg.top_k:
            longs = [c for c, r in ranked[: cfg.top_k] if r >= cfg.min_abs_return]
            shorts = [c for c, r in ranked[-cfg.top_k:] if r <= -cfg.min_abs_return]
        desired: dict[str, str] = {c: "B" for c in longs}
        desired.update({c: "A" for c in shorts})

        # ---- exits ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (now_ms - pos["ts_ms"]) / 3_600_000
            rank = rank_top.get(coin) if is_long else rank_bot.get(coin)
            reason = None
            if ret_pct <= -cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif rank is None:
                reason = "NO-SIGNAL (left ranked universe)"
            elif rank > cfg.exit_rank:
                end = "top" if is_long else "bottom"
                reason = f"RANK-OUT (#{rank} from {end}, band {cfg.exit_rank})"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"XMOM EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry,
                                     "ret_pct": ret_pct, "rank": rank},
                ))

        # ---- entries: fill empty target slots, dollar-neutral per leg ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        cooldown_ms = cfg.reentry_cooldown_hours * 3_600_000
        room = cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = cfg.max_total_notional - active_notional

        for coin, side in desired.items():
            if room <= 0 or room_notional < 5.0:
                break
            if coin in active_after:
                continue
            if now_ms - last_flat_ms.get(coin, -10**15) < cooldown_ms:
                continue
            mid = view.mids.get(coin)
            if not mid:
                continue
            notional = min(cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            direction = "long" if side == "B" else "short"
            r = sign * rets[coin]   # raw trailing return for the audit trail
            kind = "reversal rank" if cfg.invert else "rank"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"XMOM ENTER {direction} {coin} @ ${mid:.4f} "
                    f"ret {r*100:+.2f}% over {cfg.lookback_bars} bars"
                    f" ({kind} {rank_top[coin]}/{len(ranked)}), notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "trailing_return": r,
                                 "rank": rank_top[coin], "universe": len(ranked),
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            state = (
                f"book steady ({len(open_pos)} legs)" if open_pos else "no xmom book"
            )
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"{state}: {len(ranked)} ranked coins "
                    f"(need ≥{2*cfg.top_k}), {len(longs)}L/{len(shorts)}S desired"
                ),
                market_snapshot={"n_ranked": len(ranked),
                                 "n_longs": len(longs), "n_shorts": len(shorts)},
            ))
        return out
