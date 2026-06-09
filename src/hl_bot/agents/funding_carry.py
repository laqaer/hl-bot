"""Funding Carry — single-name maker carry (the fixed-economics FEMR).

FEMR has the right entry idea (extreme funding) but loses because it crosses the
spread as a taker and churns out via tight take-profit / short holds, so it pays
round-trip costs faster than it collects funding. This agent keeps the entry but
changes the economics:

  * enter the carry-collecting side (short if funding>0, long if funding<0),
  * HOLD to collect funding while |funding| stays extreme,
  * exit only when funding normalizes, a WIDE stop is hit, or max-hold elapses.

No take-profit churn. Intended for maker execution (confirm with ``--prefer
maker``). The wide stop caps the tail risk you accept in exchange for the carry.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class FundingCarryConfig:
    enter_funding_per_hr: float = 0.00015
    exit_funding_per_hr: float = 0.00005
    min_daily_volume_usd: float = 10_000_000.0
    stop_loss_pct: float = 0.03                 # wide: we're holding for carry
    max_hold_hours: float = 36.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 75.0
    max_concurrent_positions: int = 3


class FundingCarryAgent(Agent):
    def __init__(
        self,
        name: str = "funding_carry_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = FundingCarryConfig(
            enter_funding_per_hr=float(c.get("enter_funding_per_hr", 0.00015)),
            exit_funding_per_hr=float(c.get("exit_funding_per_hr", 0.00005)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.03)),
            max_hold_hours=float(c.get("max_hold_hours", 36.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 75.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 3)),
        )
        self.conn = conn

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
                open_by_coin[coin] = {"ts_ms": r["ts_ms"], "side": r["side"],
                                      "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0)}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        funding = view.funding or {}
        # enter/exit thresholds + APR are per-hour; the backtest data layer scales
        # Frame.funding by bar length. Normalize back to per-hour so one config is
        # interval-invariant (live HL funding is hourly → no-op). See xfund_carry.
        bar_hours = float(view.extra.get("bar_hours", 1.0) or 1.0)
        if bar_hours != 1.0:
            funding = {c: f / bar_hours for c, f in funding.items()}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- exits ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (time.time() - pos["ts_ms"] / 1000) / 3600
            f = funding.get(coin)
            reason = None
            if ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif f is not None and abs(f) < self.cfg.exit_funding_per_hr:
                reason = f"FUNDING-NORMALIZED ({f*100:+.4f}%/hr)"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"CARRY EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "ret_pct": ret_pct, "funding": f},
                ))

        # ---- entries ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        candidates = [
            (c, f) for c, f in funding.items()
            if c not in active_after and abs(f) >= self.cfg.enter_funding_per_hr
            and vol.get(c, 0) >= self.cfg.min_daily_volume_usd and (view.mids.get(c) or 0) > 0
        ]
        candidates.sort(key=lambda kv: abs(kv[1]), reverse=True)

        for coin, f in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            mid = view.mids[coin]
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            side = "A" if f > 0 else "B"          # collect funding
            direction = "short" if side == "A" else "long"
            apr = f * 8760 * 100
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side, sz=sz, px=mid,
                cloid=make_cloid(self.name),
                reasoning=(
                    f"CARRY ENTER {direction} {coin} @ ${mid:.4f} "
                    f"funding {f*100:+.4f}%/hr ({apr:+.0f}% APR), notional ${notional:.2f}"
                ),
                market_snapshot={"funding": f, "mid": mid, "annualized_pct": apr, "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no |funding|>={self.cfg.enter_funding_per_hr*100:.4f}%/hr on liquid coins",
                market_snapshot={},
            ))
        return out
