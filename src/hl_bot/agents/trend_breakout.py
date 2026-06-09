"""Trend Breakout — Donchian-channel trend-follow on majors (maker).

Every signal class tried so far on this book is mean-reverting or carry:
TWAP fades deviations, FEMR/xfund/funding_carry collect funding. B1c+B1d(iii)
showed the *carry* class is structurally negative net-of-cost on liquid alts, so
B1d pivots to a NON-carry, NON-fade signal: pure trend-following on the majors.

The classic, parameter-light trend entry is a Donchian breakout: go LONG when
price prints a new N-bar high, SHORT on a new N-bar low — i.e. trade *with* the
move, the opposite of TWAP. Exits ride the trend via a shorter Donchian channel
(an N-bar trailing stop), with a wide hard stop and a max-hold backstop. Designed
for maker execution (confirm with ``--prefer maker``); the wide stop is the price
of letting winners run.

It needs a trailing close series per coin in ``view.extra['closes'][coin]`` (the
backtest data loader and the live ``_enrich_view`` both populate it); the last
element is the current bar's close, which equals ``view.mids[coin]``.
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
class TrendBreakoutConfig:
    entry_lookback: int = 24            # new N-bar high/low triggers entry
    exit_lookback: int = 12            # opposite M-bar channel = trailing exit
    min_daily_volume_usd: float = 10_000_000.0
    stop_loss_pct: float = 0.05        # wide: trend-follow needs room
    max_hold_hours: float = 96.0
    max_notional_per_trade: float = 100.0
    max_total_notional: float = 300.0
    max_concurrent_positions: int = 4


class TrendBreakoutAgent(Agent):
    def __init__(
        self,
        name: str = "trend_breakout_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = TrendBreakoutConfig(
            entry_lookback=int(c.get("entry_lookback", 24)),
            exit_lookback=int(c.get("exit_lookback", 12)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.05)),
            max_hold_hours=float(c.get("max_hold_hours", 96.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 100.0)),
            max_total_notional=float(c.get("max_total_notional", 300.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 4)),
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
        closes_by_coin: dict[str, list[float]] = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- exits: trailing Donchian channel / stop / max-hold ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (time.time() - pos["ts_ms"] / 1000) / 3600
            prior = (closes_by_coin.get(coin) or [])[:-1]  # exclude current bar
            window = prior[-self.cfg.exit_lookback:]
            reason = None
            if ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif len(window) >= self.cfg.exit_lookback:
                if is_long and mid <= min(window):
                    reason = f"TRAIL-EXIT long < {self.cfg.exit_lookback}-bar low"
                elif (not is_long) and mid >= max(window):
                    reason = f"TRAIL-EXIT short > {self.cfg.exit_lookback}-bar high"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"TREND EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "ret_pct": ret_pct},
                ))

        # ---- entries: new N-bar high (long) / low (short) ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        candidates: list[tuple[str, float, str, float]] = []
        for coin, closes in closes_by_coin.items():
            if coin in active_after:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            prior = closes[:-1]  # breakout vs bars BEFORE the current one
            window = prior[-self.cfg.entry_lookback:]
            if len(window) < self.cfg.entry_lookback:
                continue
            hi, lo = max(window), min(window)
            if mid > hi:
                breakout = (mid - hi) / hi if hi > 0 else 0.0
                candidates.append((coin, breakout, "B", mid))
            elif mid < lo:
                breakout = (lo - mid) / lo if lo > 0 else 0.0
                candidates.append((coin, breakout, "A", mid))
        # rank by breakout strength (strongest new high/low first)
        candidates.sort(key=lambda r: r[1], reverse=True)

        for coin, breakout, side, mid in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            direction = "long" if side == "B" else "short"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side, sz=sz, px=mid,
                cloid=make_cloid(self.name),
                reasoning=(
                    f"TREND ENTER {direction} {coin} @ ${mid:.4f} "
                    f"({self.cfg.entry_lookback}-bar breakout {breakout*100:+.2f}%), "
                    f"notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "breakout": breakout, "side": side,
                                 "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"no {self.cfg.entry_lookback}-bar breakout among {len(closes_by_coin)} coins",
                market_snapshot={},
            ))
        return out
