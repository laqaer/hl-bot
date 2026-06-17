"""OI-spike crowding reversal (S8) — fade an overshoot a crowd just piled into.

The structural thesis behind the confirmed dislocation edge: forced/emotional
flow overshoots and reverts. ``funding_crowding_fade`` (D2a) uses *funding
extremity* as the crowding gate — the OI-free subset of this strategy. S8 adds
the signal that funding only proxies: **open interest spiking**. When OI grows
fast (new positions piling in over the last ~30min) AND price has gapped away
from its 5m VWAP, the move is crowded and tends to revert — so fade it.

Unlike funding (which is signed, so it picks the crowded SIDE), an OI spike is
*unsigned* — it only says "a lot of new positioning, fast". The direction
therefore comes from the **overshoot** itself:

  * OI spiked AND mid gapped ABOVE its 5m VWAP (z >= +z_enter)  -> SHORT,
  * OI spiked AND mid gapped BELOW (z <= -z_enter)              -> LONG.

OI is NOT in candle history (only ``metaAndAssetCtxs``), so this edge is
confirmable ONLY forward: the per-bar OI-change is accrued into ``frame_samples``
(P1) and replayed by ``confirm``. Signals (identical units in backtest & live):

  * ``view.extra['oi_change'][coin]`` — fractional OI growth over the crowding
    lookback (``ingest.accrual.build_oi_change_view`` live; ``Frame.oi_change``
    in the backtest).
  * ``view.extra['candles_5m'][coin] = {'vwap','sigma'}`` — same 5m/5h basis as
    dislocation / funding_crowding_fade.

Perp-only, TAKER entry (a reversion must get in now); a tight stop bounds the
"the crowd was right" tail.
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
class OICrowdingReversalConfig:
    oi_spike_min: float = 0.005     # fade only when OI grew >= this fraction over the lookback
    z_enter: float = 2.0            # AND |(mid-vwap)/sigma| >= this (the overshoot to fade)
    z_exit: float = 0.5            # take profit when z reverts within this of vwap
    stop_pct: float = 0.02          # hard stop bounds the negative-skew tail
    max_hold_bars: int = 12         # ~60min at 5m; reversion horizon
    bar_seconds: int = 300          # 5m
    lookback_s: float = 1800.0      # OI growth lookback (must match live accrual + backtest overlay)
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 75.0
    max_concurrent_positions: int = 3


class OICrowdingReversalAgent(Agent):
    default_execution = "taker"  # reversion must get in now (same as dislocation)

    def __init__(
        self,
        name: str = "oi_crowding_reversal_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = OICrowdingReversalConfig(
            oi_spike_min=float(c.get("oi_spike_min", 0.005)),
            z_enter=float(c.get("z_enter", 2.0)),
            z_exit=float(c.get("z_exit", 0.5)),
            stop_pct=float(c.get("stop_pct", 0.02)),
            max_hold_bars=int(c.get("max_hold_bars", 12)),
            bar_seconds=int(c.get("bar_seconds", 300)),
            lookback_s=float(c.get("lookback_s", 1800.0)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
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

    def _zscore(self, view: MarketView, coin: str) -> float | None:
        stats = (view.extra.get("candles_5m", {}) or {}).get(coin) or {}
        vwap = stats.get("vwap")
        sigma = stats.get("sigma")
        mid = view.mids.get(coin)
        if not (mid and vwap and sigma and sigma > 0):
            return None
        return (mid - vwap) / sigma

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        oi_change: dict[str, float] = view.extra.get("oi_change", {}) or {}
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
                    reasoning=f"OICROWD EXIT {coin}: {reason}",
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

        candidates: list[tuple[str, float, float, float]] = []
        for coin in view.mids:
            if coin in active_after or coin in flattening:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            oic = oi_change.get(coin)
            if oic is None or oic < self.cfg.oi_spike_min:
                continue  # no crowding (OI not spiking)
            z = self._zscore(view, coin)
            if z is None or abs(z) < self.cfg.z_enter:
                continue  # no overshoot to fade
            candidates.append((coin, z, oic, mid))
        # fade the most crowded (largest OI spike) first
        candidates.sort(key=lambda r: r[2], reverse=True)

        for coin, z, oic, mid in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            # gapped UP (z>0) -> SHORT (A); gapped DOWN -> LONG (B).
            side = "A" if z > 0 else "B"
            direction = "short" if side == "A" else "long"
            sz = round(notional / mid, 5)
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"OICROWD {direction} {coin} z={z:+.1f} ΔOI={oic*100:+.0f}% "
                    f"@ ${mid:.4f} notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "z": z, "oi_change": oic, "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no crowded overshoots (ΔOI>={self.cfg.oi_spike_min*100:.0f}% "
                    f"& |z|>={self.cfg.z_enter}); {len(candidates)} candidates, "
                    f"{len(active)} held"
                ),
                market_snapshot={"n_candidates": len(candidates), "n_held": len(active)},
            ))
        return out
