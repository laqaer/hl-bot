"""Funding-gated crowding fade — fade an overshoot the funding rate confirms.

D2a research ("funding-settlement snap") found that the snap is NOT about the
hourly settlement clock at all: it is a *crowding* fade. When a coin's funding
rate is meaningfully above the ~11% APR baseline (§0.5), longs are paying up to
hold — the trade is crowded — and the price has usually overshot in that
direction. Fading that overshoot reverts ~30-60min later. Firing on *any* 5m bar
(not just the top of the hour) gives ~10× the trades at similar per-trade edge,
and it survives a hard stop and walk-forward (research/results notes).

This is the OI-free, fundable subset of S8 (crowding_reversal): the multi-signal
crowding gate becomes just *funding extremity* + *vol-normalized overshoot* —
the two signals we actually have on HL without an OI history feed.

  * funding > +threshold (crowded LONG) AND mid gapped ABOVE its 5m VWAP
    (z >= +z_enter)  -> SHORT (fade the overshoot),
  * funding < -threshold (crowded SHORT) AND mid gapped BELOW (z <= -z_enter)
    -> LONG.

Distinct from dislocation_reversion (which fires at |z|>=3 with NO funding
gate): here the funding gate supplies the selectivity, so a much milder
overshoot (|z|~1) is tradeable. Only ~1% of these entries reach |z|>=3, so the
two edges barely overlap.

Signals (identical units in backtest and live):
  * ``view.extra['funding_hourly'][coin]`` — unscaled 1h funding rate (NOT the
    per-bar ``view.funding``, which is hourly/12 in a 5m backtest).
  * ``view.extra['candles_5m'][coin] = {'vwap','sigma'}`` — same 5m/5h basis as
    dislocation/twap_mr.

Perp-only, TAKER entry (a reversion must get in now; resting maker suffers
adverse selection), tight stop bounds the "the crowd was right" tail.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision

HOURS_PER_YEAR = 24 * 365


@dataclass
class FundingCrowdingFadeConfig:
    funding_min_apr: float = 20.0   # fade only when |funding| >= this APR% (>> 11% baseline)
    z_enter: float = 1.0            # AND |(mid-vwap)/sigma| >= this, aligned with funding sign
    z_exit: float = 0.5             # take profit when z reverts within this of vwap
    stop_pct: float = 0.02          # hard stop bounds the negative-skew tail
    max_hold_bars: int = 12         # ~60min at 5m; reversion horizon from the research
    bar_seconds: int = 300          # 5m
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 75.0
    max_concurrent_positions: int = 3


class FundingCrowdingFadeAgent(Agent):
    default_execution = "taker"  # reversion must get in now (same as dislocation)

    def __init__(
        self,
        name: str = "funding_crowding_fade_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = FundingCrowdingFadeConfig(
            funding_min_apr=float(c.get("funding_min_apr", 20.0)),
            z_enter=float(c.get("z_enter", 1.0)),
            z_exit=float(c.get("z_exit", 0.5)),
            stop_pct=float(c.get("stop_pct", 0.02)),
            max_hold_bars=int(c.get("max_hold_bars", 12)),
            bar_seconds=int(c.get("bar_seconds", 300)),
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
        candles: dict[str, dict] = view.extra.get("candles_5m", {}) or {}
        stats = candles.get(coin) or {}
        vwap = stats.get("vwap")
        sigma = stats.get("sigma")
        mid = view.mids.get(coin)
        if not (mid and vwap and sigma and sigma > 0):
            return None
        return (mid - vwap) / sigma

    def _funding_apr(self, view: MarketView, coin: str) -> float | None:
        fh: dict[str, float] = view.extra.get("funding_hourly", {}) or {}
        rate = fh.get(coin)
        if rate is None:
            return None
        return rate * HOURS_PER_YEAR * 100.0

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
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
                    reasoning=f"CROWDFADE EXIT {coin}: {reason}",
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
            apr = self._funding_apr(view, coin)
            if apr is None or abs(apr) < self.cfg.funding_min_apr:
                continue
            z = self._zscore(view, coin)
            if z is None or abs(z) < self.cfg.z_enter:
                continue
            # overshoot must align with the crowding direction: positive funding
            # (crowded long) + price gapped UP (z>0), or negative + gapped down.
            if (z > 0) != (apr > 0):
                continue
            candidates.append((coin, z, apr, mid))
        # fade the most crowded (highest |funding|) first
        candidates.sort(key=lambda r: abs(r[2]), reverse=True)

        for coin, z, apr, mid in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            # crowded long (apr>0, z>0) -> SHORT (A); crowded short -> LONG (B).
            side = "A" if apr > 0 else "B"
            direction = "short" if side == "A" else "long"
            sz = round(notional / mid, 5)
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"CROWDFADE {direction} {coin} z={z:+.1f} fundingAPR={apr:+.0f}% "
                    f"@ ${mid:.4f} notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "z": z, "funding_apr": apr, "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no crowded overshoots (|funding|>={self.cfg.funding_min_apr:.0f}% "
                    f"& |z|>={self.cfg.z_enter}); {len(candidates)} candidates, "
                    f"{len(active)} held"
                ),
                market_snapshot={"n_candidates": len(candidates), "n_held": len(active)},
            ))
        return out
