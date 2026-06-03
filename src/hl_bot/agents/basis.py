"""Basis — Cross-Perp/Spot Basis agent.

Strategy: when HL perp price diverges from HL spot price for a coin that has
both markets (BTC, ETH, SOL), trade the perp side of the convergence:

  perp_mid > spot_mid by >0.20% -> SHORT perp (rich premium)
  perp_mid < spot_mid by >0.20% -> LONG perp  (deep discount)

Exit when basis collapses to |b|<0.05%, OR 6h max hold, OR ±1% stop.

Auxiliary data: view.extra['spot_mids'] = { 'BTC': float, 'ETH': float, 'SOL': float }
populated by the runtime from spotMetaAndAssetCtxs.
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

BASIS_COINS = ("BTC", "ETH", "SOL")


@dataclass
class BasisConfig:
    enter_basis: float = 0.002      # 0.20%
    exit_basis: float = 0.0005      # 0.05%
    stop_loss_pct: float = 0.01
    max_hold_hours: float = 6.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 50.0
    max_concurrent_positions: int = 3  # at most one per BTC/ETH/SOL


class BasisAgent(Agent):
    def __init__(
        self,
        name: str = "basis_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = BasisConfig(
            enter_basis=float(c.get("enter_basis", 0.002)),
            exit_basis=float(c.get("exit_basis", 0.0005)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.01)),
            max_hold_hours=float(c.get("max_hold_hours", 6.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 50.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 3)),
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
                }
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        spot: dict[str, float] = view.extra.get("spot_mids", {}) or {}
        open_pos = self._open_positions()

        # ---- exits ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin); spot_mid = spot.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]; is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (time.time() - pos["ts_ms"] / 1000) / 3600
            basis = (mid - spot_mid) / spot_mid if spot_mid else None
            reason = None
            if ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif basis is not None and abs(basis) < self.cfg.exit_basis:
                reason = f"BASIS-COLLAPSED b={basis*1e4:+.1f}bps"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"BASIS EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "basis": basis},
                ))

        # ---- entries ----
        active = set(open_pos.keys())
        room_notional = self.cfg.max_total_notional - len(active) * self.cfg.max_notional_per_trade

        candidates = []
        for coin in BASIS_COINS:
            if coin in active:
                continue
            mid = view.mids.get(coin); spot_mid = spot.get(coin)
            if not (mid and spot_mid and mid > 0 and spot_mid > 0):
                continue
            basis = (mid - spot_mid) / spot_mid
            if abs(basis) < self.cfg.enter_basis:
                continue
            candidates.append((coin, basis, mid, spot_mid))
        candidates.sort(key=lambda r: abs(r[1]), reverse=True)

        for coin, basis, mid, spot_mid in candidates:
            if room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            sz = round(notional / mid, 5)
            # perp premium (basis > 0): short the perp
            side = "A" if basis > 0 else "B"
            direction = "short" if side == "A" else "long"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"BASIS ENTER {direction} perp {coin} @ ${mid:.2f} "
                    f"spot=${spot_mid:.2f} basis={basis*1e4:+.1f}bps"
                ),
                market_snapshot={"perp_mid": mid, "spot_mid": spot_mid, "basis": basis,
                                 "notional": notional},
            ))
            room_notional -= notional

        if not out:
            seen_basis = {
                c: (view.mids.get(c, 0) - spot.get(c, 0)) / spot[c] * 1e4
                for c in BASIS_COINS if spot.get(c)
            }
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no basis>{self.cfg.enter_basis*1e4:.0f}bps; current(bps): {seen_basis}",
                market_snapshot={"basis_bps": seen_basis},
            ))
        return out
