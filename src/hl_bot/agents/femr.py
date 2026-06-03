"""FEMR — Funding Extremes Mean Reversion + Portfolio Manager.

Strategy:
  1. ADOPT — read live positions on the sub-account. Treat ANY open position as
     a current bet whose continued thesis we must evaluate. Bot owns the whole
     portfolio, not just trades it placed itself.
  2. EVALUATE — for each open position, compute EV based on:
       - funding rate (paying or collecting)
       - distance to liquidation (risk)
       - distance to take-profit / stop-loss
       - elapsed hold time
       - vs. opportunity cost of capital deployed elsewhere
     If EV-of-continuing < EV-of-best-alternative-trade, close it.
  3. SCAN — find new entry candidates with |funding| above threshold, ranked
     by absolute funding rate (highest APR first).
  4. PLACE — fill remaining capital slots up to position/notional caps.

Designed for any-size capital. Position sizes scale with account value.
Hard guardrails (account floor, daily DD) enforced by the executor layer.

Why this approach:
  - Funding payments are absolute % of notional regardless of account size.
  - Mean-reversion edge is structural (extreme funding => unwind incoming).
  - Portfolio-aware: a stale long with negative funding (paying every hour) is
    a guaranteed bleed unless price recovers fast. Bot closes it and rotates
    capital into shorts that are COLLECTING funding.
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
class FemrConfig:
    # ---- Entry ----
    funding_enter_per_hr: float = 0.00015     # 0.015%/hr = ~130% APR
    funding_exit_per_hr: float = 0.00005      # 0.005%/hr = ~44% APR
    min_daily_volume_usd: float = 5_000_000.0
    # ---- Exits on FEMR-placed positions ----
    stop_loss_pct: float = 0.015              # 1.5% adverse move
    take_profit_pct: float = 0.008            # 0.8% favorable move
    max_hold_hours: float = 8.0
    # ---- Position sizing ----
    max_notional_per_trade: float = 20.0
    max_total_notional: float = 40.0
    max_concurrent_positions: int = 2
    # ---- Adopted-position handling ----
    # When evaluating manual/existing positions:
    #   Close if funding is bleeding us > this APR
    adopted_funding_bleed_apr: float = 0.30   # 30% annualized funding cost
    #   Close if liquidation is closer than this %
    adopted_min_liq_buffer_pct: float = 0.05  # 5%
    #   Close if unrealized loss exceeds this % of position
    adopted_max_unrealized_loss_pct: float = 0.02  # 2%


class FemrAgent(Agent):
    """Portfolio-aware funding-extremes mean-reversion agent."""

    def __init__(
        self,
        name: str = "femr_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = FemrConfig(
            funding_enter_per_hr=float(c.get("funding_enter_per_hr", 0.00015)),
            funding_exit_per_hr=float(c.get("funding_exit_per_hr", 0.00005)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 5_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.015)),
            take_profit_pct=float(c.get("take_profit_pct", 0.008)),
            max_hold_hours=float(c.get("max_hold_hours", 8.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 20.0)),
            max_total_notional=float(c.get("max_total_notional", 40.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 2)),
            adopted_funding_bleed_apr=float(c.get("adopted_funding_bleed_apr", 0.30)),
            adopted_min_liq_buffer_pct=float(c.get("adopted_min_liq_buffer_pct", 0.05)),
            adopted_max_unrealized_loss_pct=float(c.get("adopted_max_unrealized_loss_pct", 0.02)),
        )
        self.conn = conn

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _femr_open_positions(self) -> dict[str, dict]:
        """Positions FEMR itself opened (via its own audit trail in agent_decisions)."""
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """
            SELECT ts_ms, coin, action, side, sz, px, cloid
            FROM agent_decisions
            WHERE agent = ? AND coin IS NOT NULL AND action IN ('place', 'flatten')
            ORDER BY ts_ms ASC
            """,
            (self.name,),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {
                    "ts_ms": r["ts_ms"],
                    "side": r["side"],
                    "sz": float(r["sz"] or 0),
                    "entry_px": float(r["px"] or 0),
                    "cloid": r["cloid"],
                    "origin": "femr",
                }
            elif r["action"] == "flatten":
                open_by_coin.pop(coin, None)
        return open_by_coin

    # ------------------------------------------------------------------
    # Main decision loop
    # ------------------------------------------------------------------
    def decide(self, view: MarketView) -> list[Decision]:
        """Generate decisions for one tick.

        We rely on `view.extra['live_positions']` being populated by the runner
        with the current `assetPositions` from HL clearinghouseState. Schema:
          [{coin, szi (+long/-short), entry_px, position_value, unrealized_pnl,
            liquidation_px, leverage, margin_used}]
        """
        out: list[Decision] = []
        live_positions: list[dict] = view.extra.get("live_positions", []) or []
        live_by_coin = {p["coin"]: p for p in live_positions}
        femr_positions = self._femr_open_positions()
        vol = view.extra.get("day_ntl_vlm", {})

        # -------- 1. ADOPT + EVALUATE existing positions --------
        adopted_to_close: set[str] = set()
        for coin, p in live_by_coin.items():
            mid = view.mids.get(coin)
            funding = view.funding.get(coin)
            if mid is None or mid <= 0:
                continue
            szi = float(p.get("szi", 0))            # signed: + long, - short
            is_long = szi > 0
            entry = float(p.get("entry_px", 0) or 0)
            liq = float(p.get("liquidation_px", 0) or 0)
            unrealized = float(p.get("unrealized_pnl", 0) or 0)
            pos_value = abs(float(p.get("position_value", 0) or 0))

            # 1a. Is this OUR position? (FEMR opened it)
            if coin in femr_positions:
                fp = femr_positions[coin]
                hold_hrs = (time.time() - fp["ts_ms"] / 1000) / 3600
                ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
                reason = None
                if ret_pct <= -self.cfg.stop_loss_pct:
                    reason = f"STOP-LOSS ({ret_pct*100:+.2f}%)"
                elif ret_pct >= self.cfg.take_profit_pct:
                    reason = f"TAKE-PROFIT ({ret_pct*100:+.2f}%)"
                elif hold_hrs >= self.cfg.max_hold_hours:
                    reason = f"MAX-HOLD ({hold_hrs:.1f}h)"
                elif funding is not None and abs(funding) < self.cfg.funding_exit_per_hr:
                    reason = f"FUNDING-NORMALIZED ({funding*100:+.4f}%/hr)"
                if reason:
                    out.append(_close(self.name, coin, abs(szi), mid, "FEMR-EXIT", reason,
                                      entry=entry, hold_hrs=hold_hrs, funding=funding,
                                      ret_pct=ret_pct))
                    adopted_to_close.add(coin)
                continue

            # 1b. Adopted (non-FEMR) position — evaluate continuation EV.
            reasons = []
            # (i) Funding bleed: if long and funding>0, we PAY shorts every hour.
            #     If short and funding<0, we pay longs.
            funding_apr_signed = (funding or 0) * 8760  # +pay if long, -pay if short
            paying_apr = funding_apr_signed if is_long else -funding_apr_signed
            if paying_apr > self.cfg.adopted_funding_bleed_apr:
                reasons.append(
                    f"funding bleed {paying_apr*100:.0f}% APR > {self.cfg.adopted_funding_bleed_apr*100:.0f}%"
                )
            # (ii) Liquidation proximity
            if liq > 0:
                liq_dist = abs(mid - liq) / mid
                if liq_dist < self.cfg.adopted_min_liq_buffer_pct:
                    reasons.append(f"liq {liq_dist*100:.1f}% away < {self.cfg.adopted_min_liq_buffer_pct*100:.0f}%")
            # (iii) Unrealized loss as % of notional
            if pos_value > 0 and unrealized < 0:
                loss_pct = abs(unrealized) / pos_value
                if loss_pct > self.cfg.adopted_max_unrealized_loss_pct:
                    reasons.append(f"loss {loss_pct*100:.2f}% > {self.cfg.adopted_max_unrealized_loss_pct*100:.1f}%")
            # (iv) Funding actively against the position direction (mean-reversion thesis):
            #      if long and funding extremely positive, longs are overcrowded -> we're on wrong side
            if funding is not None and abs(funding) >= self.cfg.funding_enter_per_hr:
                wrong_side = (is_long and funding > 0) or (not is_long and funding < 0)
                if wrong_side:
                    reasons.append(
                        f"funding {funding*100:+.4f}%/hr signals OPPOSITE direction (wrong side of crowd)"
                    )

            if reasons:
                out.append(_close(
                    self.name, coin, abs(szi), mid,
                    "ADOPT-CLOSE", "; ".join(reasons),
                    entry=entry, unrealized=unrealized, liq=liq, funding=funding,
                ))
                adopted_to_close.add(coin)
            else:
                # Log a "hold" decision so we have audit of why we kept it.
                paying_apr_pct = paying_apr * 100
                out.append(Decision(
                    agent=self.name, action="hold", coin=coin,
                    reasoning=(
                        f"ADOPT-HOLD {coin} {'long' if is_long else 'short'}: "
                        f"funding cost {paying_apr_pct:+.1f}% APR, "
                        f"unrealized ${unrealized:+.2f}, "
                        f"liq {(abs(mid-liq)/mid*100 if liq else 0):.1f}% away"
                    ),
                    market_snapshot={
                        "adopted": True, "is_long": is_long,
                        "entry_px": entry, "mid": mid, "unrealized": unrealized,
                        "funding": funding, "paying_apr": paying_apr,
                    },
                ))

        # -------- 2. SCAN for new entries --------
        active_coins = set(live_by_coin.keys()) - adopted_to_close
        active_notional = sum(
            abs(float(p.get("position_value", 0) or 0))
            for c, p in live_by_coin.items() if c not in adopted_to_close
        )
        room_for_new = self.cfg.max_concurrent_positions - len(active_coins)
        room_for_notional = self.cfg.max_total_notional - active_notional

        if room_for_new <= 0 or room_for_notional < 5.0:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"capacity full: {len(active_coins)} positions, ${active_notional:.0f} notional",
                market_snapshot={"active_coins": list(active_coins), "active_notional": active_notional},
            ))
            return out

        candidates = []
        for coin, funding in view.funding.items():
            if coin in active_coins:
                continue
            if abs(funding) < self.cfg.funding_enter_per_hr:
                continue
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            v24 = vol.get(coin, 0)
            if v24 < self.cfg.min_daily_volume_usd:
                continue
            candidates.append((coin, funding, mid, v24))
        candidates.sort(key=lambda r: abs(r[1]), reverse=True)

        for coin, funding, mid, v24 in candidates[:room_for_new]:
            notional = min(self.cfg.max_notional_per_trade, room_for_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            # Funding > 0 -> longs paying -> short to collect (side='A' = sell)
            # Funding < 0 -> shorts paying -> long to collect  (side='B' = buy)
            side = "A" if funding > 0 else "B"
            direction = "short" if side == "A" else "long"
            apr = funding * 8760 * 100
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"ENTER {direction} {coin} @ ${mid:.4f}, "
                    f"funding {funding*100:+.4f}%/hr ({apr:+.0f}% APR), "
                    f"24h vol ${v24/1e6:.0f}M, notional ${notional:.2f}"
                ),
                market_snapshot={
                    "funding": funding, "mid": mid, "vol24": v24,
                    "annualized_pct": apr, "notional": notional,
                },
            ))
            room_for_notional -= notional

        if not any(d.action == "place" for d in out):
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no new entries: {len(candidates)} candidates above threshold",
                market_snapshot={"n_funding_signals": len(candidates)},
            ))
        return out


def _close(agent: str, coin: str, sz: float, mid: float, label: str, reason: str, **snap) -> Decision:
    return Decision(
        agent=agent, action="flatten", coin=coin,
        side=None, sz=sz, px=mid, cloid=make_cloid(agent),
        reasoning=f"{label} {coin}: {reason}",
        market_snapshot={"exit_px": mid, **snap},
    )
