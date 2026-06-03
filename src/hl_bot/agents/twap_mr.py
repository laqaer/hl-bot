"""TWAP-MR — TWAP Mean Reversion agent.

Strategy: when a coin's mid deviates >2 sigma from its 1h VWAP, fade the move,
expecting reversion. Only on liquid coins (>$10M 24h vol).

Entry  : |mid - vwap1h| / sigma1h > 2.0
Exit   : |mid - vwap1h| / sigma1h < 0.5  OR  ±1.5% stop  OR  4h max hold

Auxiliary data: view.extra['candles_1h'] = { coin: {'vwap': float, 'sigma': float} }
Populated by the runtime by fetching 60×1m candles per top-vol coin.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision

log = logging.getLogger(__name__)


@dataclass
class TwapMrConfig:
    sigma_enter: float = 2.0
    sigma_exit: float = 0.5
    min_daily_volume_usd: float = 10_000_000.0
    stop_loss_pct: float = 0.015
    max_hold_hours: float = 4.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 50.0
    max_concurrent_positions: int = 2


class TwapMrAgent(Agent):
    def __init__(
        self,
        name: str = "twap_mr_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = TwapMrConfig(
            sigma_enter=float(c.get("sigma_enter", 2.0)),
            sigma_exit=float(c.get("sigma_exit", 0.5)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.015)),
            max_hold_hours=float(c.get("max_hold_hours", 4.0)),
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
               ORDER BY ts_ms ASC""",
            (self.name,),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {
                    "ts_ms": r["ts_ms"], "side": r["side"],
                    "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0),
                    "cloid": r["cloid"],
                }
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        candles: dict[str, dict] = view.extra.get("candles_1h", {}) or {}
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- exits on our own positions ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (time.time() - pos["ts_ms"] / 1000) / 3600
            stats = candles.get(coin) or {}
            vwap = stats.get("vwap"); sigma = stats.get("sigma") or 0
            reason = None
            if ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif vwap and sigma > 0 and abs(mid - vwap) / sigma < self.cfg.sigma_exit:
                reason = f"REVERTED z={(mid-vwap)/sigma:+.2f}"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"TWAP-MR EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "ret_pct": ret_pct},
                ))

        # ---- scan for entries ----
        active = set(open_pos.keys())
        room = self.cfg.max_concurrent_positions - len(active)
        room_notional = self.cfg.max_total_notional - len(active) * self.cfg.max_notional_per_trade
        candidates = []
        for coin, stats in candles.items():
            if coin in active:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            mid = view.mids.get(coin)
            vwap = stats.get("vwap"); sigma = stats.get("sigma")
            if not (mid and vwap and sigma and sigma > 0):
                continue
            z = (mid - vwap) / sigma
            if abs(z) < self.cfg.sigma_enter:
                continue
            candidates.append((coin, z, mid, vwap, sigma))
        # rank by extremity
        candidates.sort(key=lambda r: abs(r[1]), reverse=True)

        placed = 0
        for coin, z, mid, vwap, sigma in candidates:
            if placed >= room or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            sz = round(notional / mid, 5)
            # Fade: if mid > vwap (z>0), short. If mid<vwap (z<0), long.
            side = "A" if z > 0 else "B"
            direction = "short" if side == "A" else "long"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"TWAP-MR ENTER {direction} {coin} @ ${mid:.4f} "
                    f"z={z:+.2f} vwap=${vwap:.4f} sigma=${sigma:.4f} "
                    f"vol24=${vol.get(coin,0)/1e6:.0f}M"
                ),
                market_snapshot={"mid": mid, "vwap": vwap, "sigma": sigma, "z": z,
                                 "vol24": vol.get(coin, 0), "notional": notional},
            ))
            placed += 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no z>{self.cfg.sigma_enter} signals among {len(candles)} coins w/ candles",
                market_snapshot={"n_candle_coins": len(candles), "n_active": len(active)},
            ))
        return out
