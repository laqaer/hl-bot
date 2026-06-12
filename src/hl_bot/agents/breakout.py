"""Breakout — time-series momentum (Donchian channel) agent (B-EDGE2).

The book's only validated edge is mean reversion (twap_mr), which bleeds in
exactly the trending tapes a fader can't avoid. Channel breakout is the
canonical low-correlation complement: go long when price clears the prior
N-bar high, short when it breaks the prior N-bar low, ride until the move
stalls. Where twap_mr sells strength, this buys it — by construction the two
make money in opposite regimes.

Entry : mid breaks the prior ``lookback_bars`` close-channel by more than
        ``min_break_pct``. Liquidity floor as twap_mr.
Exit  : mid crosses the opposite extreme of the prior ``exit_lookback_bars``
        closes (classic Donchian exit), OR ±stop_loss_pct, OR max hold.

Data  : view.extra[``closes_key``] — trailing closes per coin, current bar
        last (== mid in backtest frames). Default key 'closes' (backtest frames
        carry ``vwap_window`` closes, so a lookback of N needs --vwap-window ≥
        N+1); the live roster sets 'closes_15m' so the validated 15m-bar
        channel rides ``_enrich_view``'s dedicated 15m feed instead of the 1m
        VWAP window. Lookback is in BARS — at 1m bars 240 = a 4h channel, at
        15m bars 16 = the same 4h.
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


def channel_break(
    closes: list[float], mid: float, lookback: int, min_break_pct: float
) -> tuple[str | None, float]:
    """Side ('B'/'A') and relative strength if ``mid`` breaks the prior channel.

    The channel is the max/min of the ``lookback`` closes *before* the current
    bar (``closes[-1]`` is the in-progress bar, == mid in backtest frames), so
    a bar can't break a channel it is itself part of. Strength is the relative
    distance beyond the edge — used to rank concurrent candidates. Returns
    (None, 0.0) when there is no break or not enough history.
    """
    if lookback < 1 or len(closes) < lookback + 1 or mid <= 0:
        return None, 0.0
    channel = closes[-(lookback + 1):-1]
    hi, lo = max(channel), min(channel)
    if hi <= 0 or lo <= 0:
        return None, 0.0
    if mid > hi * (1.0 + min_break_pct):
        return "B", (mid - hi) / hi
    if mid < lo * (1.0 - min_break_pct):
        return "A", (lo - mid) / lo
    return None, 0.0


def channel_exit(
    closes: list[float], mid: float, is_long: bool, exit_lookback: int
) -> bool:
    """True when ``mid`` crosses the opposite extreme of the prior exit channel.

    A long exits below the prior ``exit_lookback``-close low, a short above the
    prior high. Too little history never exits here — the stop and max-hold
    guards still apply.
    """
    if exit_lookback < 1 or len(closes) < exit_lookback + 1 or mid <= 0:
        return False
    channel = closes[-(exit_lookback + 1):-1]
    return mid < min(channel) if is_long else mid > max(channel)


@dataclass
class BreakoutConfig:
    lookback_bars: int = 240          # entry channel length (bars)
    exit_lookback_bars: int = 60      # exit channel length (bars)
    min_break_pct: float = 0.0        # extra buffer beyond the channel edge
    min_daily_volume_usd: float = 10_000_000.0
    stop_loss_pct: float = 0.03
    max_hold_hours: float = 24.0
    reentry_cooldown_hours: float = 1.0   # no fresh entry right after an exit
    max_notional_per_trade: float = 200.0
    max_total_notional: float = float("inf")
    max_concurrent_positions: int = 5
    closes_key: str = "closes"        # view.extra key carrying trailing closes


class BreakoutAgent(Agent):
    def __init__(
        self,
        name: str = "breakout_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = BreakoutConfig(
            lookback_bars=int(c.get("lookback_bars", 240)),
            exit_lookback_bars=int(c.get("exit_lookback_bars", 60)),
            min_break_pct=float(c.get("min_break_pct", 0.0)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            stop_loss_pct=float(c.get("stop_loss_pct", 0.03)),
            max_hold_hours=float(c.get("max_hold_hours", 24.0)),
            reentry_cooldown_hours=float(c.get("reentry_cooldown_hours", 1.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 200.0)),
            max_total_notional=float(c.get("max_total_notional", float("inf"))),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 5)),
            closes_key=str(c.get("closes_key", "closes")),
        )
        self.conn = conn

    def _position_state(self) -> tuple[dict[str, dict], dict[str, int]]:
        """Replay this agent's decision log → (open positions, last flatten ts).

        Same audit-log replay as twap_mr: a 'place' opens, a 'flatten' closes.
        The last-flatten timestamps drive the re-entry cooldown so a stopped-out
        coin isn't immediately re-bought off the same stale channel.
        Replays only the book matching the current tick mode (``paper_book``).
        """
        if self.conn is None:
            return {}, {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px, cloid
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
                    "cloid": r["cloid"],
                }
            else:
                open_by_coin.pop(coin, None)
                last_flat_ms[coin] = r["ts_ms"]
        return open_by_coin, last_flat_ms

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes_by_coin: dict[str, list[float]] = (
            view.extra.get(self.cfg.closes_key, {}) or {}
        )
        vol: dict[str, float] = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos, last_flat_ms = self._position_state()
        now_ms = int(time.time() * 1000)

        # ---- exits on our own positions ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            ret_pct = (mid - entry) / entry if is_long else (entry - mid) / entry
            hold_hrs = (now_ms - pos["ts_ms"]) / 3_600_000
            reason = None
            if ret_pct <= -self.cfg.stop_loss_pct:
                reason = f"STOP {ret_pct*100:+.2f}%"
            elif hold_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD {hold_hrs:.1f}h"
            elif channel_exit(closes_by_coin.get(coin) or [], mid, is_long,
                              self.cfg.exit_lookback_bars):
                reason = f"CHANNEL-EXIT {ret_pct*100:+.2f}%"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"BREAKOUT EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry, "ret_pct": ret_pct},
                ))

        # ---- scan for entries ----
        active = set(open_pos.keys())
        cooldown_ms = self.cfg.reentry_cooldown_hours * 3_600_000
        room = self.cfg.max_concurrent_positions - len(active)
        room_notional = self.cfg.max_total_notional - len(active) * self.cfg.max_notional_per_trade
        candidates = []
        for coin, closes in closes_by_coin.items():
            if coin in active:
                continue
            if now_ms - last_flat_ms.get(coin, -10**15) < cooldown_ms:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            side, strength = channel_break(
                closes, mid, self.cfg.lookback_bars, self.cfg.min_break_pct)
            if side is None:
                continue
            candidates.append((coin, side, strength, mid))
        candidates.sort(key=lambda r: r[2], reverse=True)

        for placed, (coin, side, strength, mid) in enumerate(candidates):
            if placed >= room or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            sz = round(notional / mid, 5)
            direction = "long" if side == "B" else "short"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"BREAKOUT ENTER {direction} {coin} @ ${mid:.4f} "
                    f"break={strength*100:+.2f}% beyond {self.cfg.lookback_bars}-bar channel "
                    f"vol24=${vol.get(coin,0)/1e6:.0f}M"
                ),
                market_snapshot={"mid": mid, "break_strength": strength,
                                 "lookback_bars": self.cfg.lookback_bars,
                                 "vol24": vol.get(coin, 0), "notional": notional},
            ))
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no {self.cfg.lookback_bars}-bar channel breaks among "
                    f"{len(closes_by_coin)} coins w/ {self.cfg.closes_key}"
                ),
                market_snapshot={"n_close_coins": len(closes_by_coin),
                                 "n_active": len(active)},
            ))
        return out
