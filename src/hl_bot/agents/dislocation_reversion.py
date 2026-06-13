"""Dislocation Reversion — fade the observable EFFECT of forced flow.

The dead ``liq_cascade`` idea needed a liquidation feed to trade. This rebuilds
the same thesis WITHOUT one: liquidation cascades / stop runs cause sharp 1-5%
moves that overshoot and partially revert within minutes. The observable effect
is a violent, volatility-normalized price move away from a short-window VWAP. We
fade the EXTREME of that move on fine-grained (5m) candles:

  * mid gaps far BELOW its short-window VWAP (z <= -z_enter) -> the cascade sold
    into us; go LONG (buy the dislocation),
  * mid gaps far ABOVE (z >= +z_enter) -> go SHORT.

TAKER entry (cross the spread to get in NOW): a reversion strategy that rests
a maker bid below mid suffers adverse selection — it fills only when the move
continues against it and misses the reverting fills it wants. A tight stop
bounds the "it kept going" tail; a short max-hold caps the decay of the edge. This is a
tuned variant of ``twap_mr_regime`` (extreme z, tight stop, short hold, maker
entry) and reads its vwap/sigma signal the same way:
``view.extra['candles_1h'][coin] = {'vwap': float, 'sigma': float}``.

Perp-only (single leg) — ``coin`` is the plain perp coin, prices from
``view.mids[coin]``, funding accrues normally (negligible over short holds).
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
class DislocationReversionConfig:
    z_enter: float = 3.0            # fade when |(mid-vwap)/sigma| >= this
    z_exit: float = 0.5            # take profit when z reverts within this of vwap
    stop_pct: float = 0.015        # 1.5% adverse -> stop out
    max_hold_bars: int = 12        # ~1h at 5m; dislocation edges decay fast
    bar_seconds: int = 300         # 5m; for converting max_hold_bars to a time check
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 75.0
    max_concurrent_positions: int = 3
    # if >0, skip fading when the lookback move exceeds this (avoid fading a real
    # trend); 0 = off for v1.
    trend_guard_pct: float = 0.0


class DislocationReversionAgent(Agent):
    default_execution = "taker"  # reversion must get in now; maker-resting has
                                 # adverse selection (fills on continuation)

    def __init__(
        self,
        name: str = "dislocation_reversion_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = DislocationReversionConfig(
            z_enter=float(c.get("z_enter", 3.0)),
            z_exit=float(c.get("z_exit", 0.5)),
            stop_pct=float(c.get("stop_pct", 0.015)),
            max_hold_bars=int(c.get("max_hold_bars", 12)),
            bar_seconds=int(c.get("bar_seconds", 300)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 75.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 3)),
            trend_guard_pct=float(c.get("trend_guard_pct", 0.0)),
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
                open_by_coin[coin] = {"ts_ms": r["ts_ms"], "side": r["side"],
                                      "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0)}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def _signal(self, view: MarketView, coin: str) -> tuple[float, float] | None:
        """Return ``(z, vwap)`` for ``coin`` or ``None`` if unavailable.

        Reads vwap/sigma the SAME way twap_mr_regime/twap_mr does:
        ``view.extra['candles_1h'][coin] = {'vwap': ..., 'sigma': ...}``.
        """
        candles: dict[str, dict] = view.extra.get("candles_1h", {}) or {}
        stats = candles.get(coin) or {}
        vwap = stats.get("vwap")
        sigma = stats.get("sigma")
        mid = view.mids.get(coin)
        if not (mid and vwap and sigma and sigma > 0):
            return None
        return (mid - vwap) / sigma, vwap

    def _zscore(self, view: MarketView, coin: str) -> float | None:
        sig = self._signal(view, coin)
        return None if sig is None else sig[0]

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        closes_by_coin: dict[str, list[float]] = view.extra.get("closes", {}) or {}
        open_pos = self._open_positions()

        # ---- exits FIRST ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            adverse = (entry - mid) / entry if is_long else (mid - entry) / entry
            held_bars = (time.time() - pos["ts_ms"] / 1000) / self.cfg.bar_seconds
            z = self._zscore(view, coin)
            reason = None
            if adverse >= self.cfg.stop_pct:
                reason = f"STOP {adverse*100:+.2f}%"
            elif z is not None and abs(z) <= self.cfg.z_exit:
                reason = f"REVERTED z={z:+.2f}"
            elif z is not None and (z >= 0 if is_long else z <= 0):
                reason = f"CROSSED z={z:+.2f}"
            elif held_bars >= self.cfg.max_hold_bars:
                reason = f"MAX-HOLD {held_bars:.1f}bars"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"DISLOC EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "adverse": adverse, "z": z},
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

        candidates: list[tuple[str, float, float]] = []
        for coin in view.mids:
            if coin in active_after or coin in flattening:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            z = self._zscore(view, coin)
            if z is None or abs(z) < self.cfg.z_enter:
                continue
            if self.cfg.trend_guard_pct > 0:
                closes = closes_by_coin.get(coin) or []
                if len(closes) >= 2 and closes[0] > 0:
                    move = abs(closes[-1] - closes[0]) / closes[0]
                    if move > self.cfg.trend_guard_pct:
                        continue
            candidates.append((coin, z, mid))
        # fade the most extreme dislocation first
        candidates.sort(key=lambda r: abs(r[1]), reverse=True)

        for coin, z, mid in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            # z<=-z_enter -> dislocation DOWN -> fade LONG (B);
            # z>=+z_enter -> dislocation UP   -> fade SHORT (A).
            side = "A" if z > 0 else "B"
            direction = "short" if side == "A" else "long"
            sz = round(notional / mid, 5)
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"DISLOC ENTER {direction} {coin} z={z:.1f} @ ${mid:.4f} "
                    f"vol24=${vol.get(coin, 0)/1e6:.0f}M notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "z": z, "vol24": vol.get(coin, 0),
                                 "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no |z|>={self.cfg.z_enter} dislocations "
                    f"({len(candidates)} candidates, {len(active)} held)"
                ),
                market_snapshot={"n_candidates": len(candidates), "n_held": len(active)},
            ))
        return out
