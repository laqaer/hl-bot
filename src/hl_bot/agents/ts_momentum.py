"""TS-Momentum — time-series (absolute) momentum / trend-following.

Thesis (the structurally-*different* signal after four cross-sectional ranks were
pruned). Every signal pruned so far — TWAP-MR, funding carry (majors + alts),
cross-sectional momentum, and its regime-gated variant — was a *relative*,
dollar-neutral rank: it bets one coin against another and washes out market beta.
Time-series momentum is the orthogonal class: each coin is traded **independently
on the sign of its own trailing return** — LONG if it's trending up, SHORT if
it's trending down — so the book takes *net directional* exposure (all-long in a
broad rally, all-short in a broad sell-off). This is the canonical trend-following
(CTA) edge and the one directional strategy that is regime-*adaptive*: it flips
short in downtrends rather than fading them.

Whether net-directional trend survives costs *and* a disjoint out-of-time window
(`confirm --windows 2+`) is an empirical question — exactly the bar that pruned
the cross-sectional leads. Directional beta over a single 120d window is partly a
bet on that window's regime, so the multi-window durability test is the honest
judge, not a single trailing PASS.

Designed for maker execution (confirm with ``--prefer maker``): entries are
patient. Exit a held coin when its trend decays below the exit band or flips sign.
A ``reversion`` flag flips the signal so the same book tests single-name
short-horizon mean-reversion at no extra code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class TsMomentumConfig:
    lookback_bars: int = 24                     # trailing-return window (bars)
    enter_return: float = 0.02                  # |trailing return| to open a position
    exit_return: float = 0.005                  # exit when |trailing return| falls below this
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    reversion: bool = False                     # True: fade the trend (long down, short up)


class TsMomentumAgent(Agent):
    def __init__(
        self,
        name: str = "ts_momentum_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = TsMomentumConfig(
            lookback_bars=int(c.get("lookback_bars", 24)),
            enter_return=float(c.get("enter_return", 0.02)),
            exit_return=float(c.get("exit_return", 0.005)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
            reversion=bool(c.get("reversion", False)),
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
                open_by_coin[coin] = {"side": r["side"], "sz": float(r["sz"] or 0),
                                      "entry_px": float(r["px"] or 0), "ts_ms": r["ts_ms"]}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    @staticmethod
    def _trailing_return(closes: list[float], lb: int) -> float | None:
        if not closes or len(closes) < lb + 1:
            return None
        past = closes[-(lb + 1)]
        if past <= 0:
            return None
        return (closes[-1] - past) / past

    def _signal(self, closes: list[float]) -> float | None:
        """Signed trend over ``lookback_bars`` (``reversion`` flips it). None if short."""
        ret = self._trailing_return(closes, self.cfg.lookback_bars)
        if ret is None:
            return None
        return -ret if self.cfg.reversion else ret

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- per-coin trend signal (independent; no cross-sectional ranking) ----
        signal: dict[str, float] = {}
        for coin, series in closes.items():
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            m = self._signal(series)
            if m is not None:
                signal[coin] = m

        # desired side per coin: long an up-trend, short a down-trend, beyond the band
        desired: dict[str, str] = {}
        for coin, m in signal.items():
            if m >= self.cfg.enter_return:
                desired[coin] = "B"
            elif m <= -self.cfg.enter_return:
                desired[coin] = "A"

        # ---- exits: trend decayed below the band, flipped sign, or left the set ----
        for coin, pos in list(open_pos.items()):
            m = signal.get(coin)
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if m is not None and abs(m) < self.cfg.exit_return:
                reason = f"TREND-DECAYED ({m*100:+.2f}%)"
            elif want is None:
                reason = "TREND left the band"
            elif want != pos["side"]:
                reason = "TREND FLIPPED — wrong side now"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"TSMOM EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "signal": m},
                ))

        # ---- entries: open trending coins up to the notional / concurrency caps ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        # strongest trends first so the caps fund the highest-conviction names
        for coin, side in sorted(desired.items(), key=lambda kv: -abs(signal[kv[0]])):
            if room <= 0 or room_notional < 5.0:
                break
            if coin in active_after:
                continue
            mid = view.mids.get(coin)
            m = signal.get(coin)
            if not mid or m is None:
                continue
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            direction = "short" if side == "A" else "long"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side, sz=sz, px=mid,
                cloid=make_cloid(self.name),
                reasoning=(
                    f"TSMOM ENTER {direction} {coin} @ ${mid:.4f} "
                    f"{self.cfg.lookback_bars}b-return {m*100:+.2f}%, notional ${notional:.2f}"
                ),
                market_snapshot={"signal": m, "mid": mid,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            n_long = sum(1 for s in desired.values() if s == "B")
            n_short = sum(1 for s in desired.values() if s == "A")
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no trend: {n_long} up / {n_short} down beyond "
                    f"{self.cfg.enter_return*100:.2f}% over {self.cfg.lookback_bars}b"
                ),
                market_snapshot={"n_longs": n_long, "n_shorts": n_short},
            ))
        return out
