"""Liq-Cascade — momentum on liquidation events.

Strategy: when a big liquidation hits (>$100k in last 5min) on a top-vol coin,
enter SAME direction as the cascade. Thesis: more forced unwinds in coming
minutes will push price further. Quick in, quick out.

Entry  : cumulative_5m_liq_notional > $100k on a top-20-volume coin
Direction: long if SHORTS got liquidated (forced buybacks => up)
           short if LONGS got liquidated (forced sells => down)
Exit   : 30min max hold, +0.5% TP, -1.5% SL

Auxiliary data: view.extra['liquidations'] = list of {coin, side, notional_usd, ts_ms}
"side" follows HL convention: side of the LIQUIDATED order. 'A'=sells (longs liq'd),
'B'=buys (shorts liq'd). We trade SAME side as that resolved liquidation pressure.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision

log = logging.getLogger(__name__)


@dataclass
class LiqCascadeConfig:
    min_liq_notional_usd: float = 100_000.0
    min_daily_volume_usd: float = 10_000_000.0
    window_s: int = 300
    take_profit_pct: float = 0.005
    stop_loss_pct: float = 0.015
    max_hold_minutes: float = 30.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 50.0
    max_concurrent_positions: int = 2


class LiqCascadeAgent(Agent):
    def __init__(
        self,
        name: str = "liq_cascade_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = LiqCascadeConfig(
            min_liq_notional_usd=float(c.get("min_liq_notional_usd", 100_000.0)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            window_s=int(c.get("window_s", 300)),
            take_profit_pct=float(c.get("take_profit_pct", 0.005)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.015)),
            max_hold_minutes=float(c.get("max_hold_minutes", 30.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 50.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 2)),
        )
        self.conn = conn

    def _open_positions(self) -> dict[str, dict]:
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px, cloid
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
                open_by_coin[coin] = {
                    "ts_ms": r["ts_ms"], "side": r["side"],
                    "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0),
                }
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        liqs: list[dict] = view.extra.get("liquidations", []) or []
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- exits ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_min = (time.time() - pos["ts_ms"] / 1000) / 60
            reason = None
            if ret_pct >= self.cfg.take_profit_pct:
                reason = f"TP {ret_pct*100:+.2f}%"
            elif ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"SL {ret_pct*100:+.2f}%"
            elif hold_min >= self.cfg.max_hold_minutes:
                reason = f"MAX-HOLD {hold_min:.0f}m"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"LIQ-CASCADE EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "ret_pct": ret_pct},
                ))

        # ---- aggregate liquidations in window ----
        now_ms = view.ts_ms
        cutoff_ms = now_ms - self.cfg.window_s * 1000
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"A": 0.0, "B": 0.0})
        for evt in liqs:
            ts = int(evt.get("ts_ms") or evt.get("time") or 0)
            if ts < cutoff_ms:
                continue
            coin = evt.get("coin")
            side = evt.get("side")  # side of liquidated order
            ntl = float(evt.get("notional_usd") or 0)
            if coin and side in ("A", "B") and ntl > 0:
                agg[coin][side] += ntl

        active = set(open_pos.keys())
        room = self.cfg.max_concurrent_positions - len(active)
        room_notional = self.cfg.max_total_notional - len(active) * self.cfg.max_notional_per_trade

        signals = []
        for coin, sides in agg.items():
            if coin in active:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            # Net dominant side
            net = sides["A"] + sides["B"]
            if net < self.cfg.min_liq_notional_usd:
                continue
            # Trade in direction of cascade pressure:
            # liquidated longs (side 'A' = forced sell) -> price down -> SHORT (side 'A')
            # liquidated shorts (side 'B' = forced buy) -> price up -> LONG (side 'B')
            dominant_side = "A" if sides["A"] > sides["B"] else "B"
            signals.append((coin, dominant_side, net))
        signals.sort(key=lambda r: r[2], reverse=True)

        placed = 0
        for coin, side, ntl in signals:
            if placed >= room or room_notional < 5.0:
                break
            mid = view.mids.get(coin)
            if not mid:
                continue
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            sz = round(notional / mid, 5)
            direction = "long" if side == "B" else "short"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"LIQ-CASCADE ENTER {direction} {coin} @ ${mid:.4f} "
                    f"liq5m=${ntl/1e3:.0f}k vol24=${vol.get(coin,0)/1e6:.0f}M"
                ),
                market_snapshot={"mid": mid, "liq_5m_usd": ntl, "side": side,
                                 "notional": notional, "vol24": vol.get(coin, 0)},
            ))
            placed += 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no liq>{self.cfg.min_liq_notional_usd/1e3:.0f}k in last "
                          f"{self.cfg.window_s}s ({len(liqs)} events seen)",
                market_snapshot={"n_events": len(liqs), "n_coins_with_liqs": len(agg)},
            ))
        return out
