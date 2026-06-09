"""X-Sectional Momentum — market-neutral cross-sectional price momentum.

Thesis (the structurally-different signal after carry and TWAP-MR were pruned):
relative strength persists. Rank the universe by trailing return over a lookback
window; hold a dollar-neutral book — LONG the top-K strongest, SHORT the bottom-K
weakest — and the spread (winners keep winning, losers keep losing) is the edge,
net of (maker) costs. Directional beta washes out, so it scales with capital and
tolerates the 5-min loop (the signal horizon is hours-to-days, not seconds).

A single ``reversion`` flag flips the sign so the *same* book tests the opposite
thesis — short-horizon cross-sectional *mean reversion* (LONG the losers, SHORT
the winners) — at no extra code. Crypto perps momentum-continue on multi-day
horizons but mean-revert intraday; which one (if either) survives costs is an
empirical question for the confirm harness, not a guess.

Designed for maker execution (confirm with ``--prefer maker``): entries are
patient. Exit a held coin when it leaves the target set (rank rotated / momentum
decayed below the exit band) or its momentum flips to the wrong sign.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class XSectMomentumConfig:
    lookback_bars: int = 24                     # trailing-return window (bars)
    enter_return: float = 0.02                  # |trailing return| to be eligible for a leg
    exit_return: float = 0.005                  # exit when |trailing return| falls below this
    top_k: int = 2                              # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    reversion: bool = False                     # True: long losers / short winners


class XSectMomentumAgent(Agent):
    def __init__(
        self,
        name: str = "xsect_momentum_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = XSectMomentumConfig(
            lookback_bars=int(c.get("lookback_bars", 24)),
            enter_return=float(c.get("enter_return", 0.02)),
            exit_return=float(c.get("exit_return", 0.005)),
            top_k=int(c.get("top_k", 2)),
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

    def _momentum(self, closes: list[float]) -> float | None:
        """Trailing return over ``lookback_bars`` (signed). None if too short."""
        lb = self.cfg.lookback_bars
        if not closes or len(closes) < lb + 1:
            return None
        past = closes[-(lb + 1)]
        last = closes[-1]
        if past <= 0:
            return None
        ret = (last - past) / past
        return -ret if self.cfg.reversion else ret

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        signal: dict[str, float] = {}
        for coin, series in closes.items():
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            m = self._momentum(series)
            if m is not None:
                signal[coin] = m

        ranked = sorted(signal.items(), key=lambda kv: kv[1])
        longs = [c for c, m in reversed(ranked) if m >= self.cfg.enter_return][: self.cfg.top_k]
        shorts = [c for c, m in ranked if m <= -self.cfg.enter_return][: self.cfg.top_k]
        desired: dict[str, str] = {c: "B" for c in longs}
        desired.update({c: "A" for c in shorts})

        # ---- exits: leave the book when a coin drops out of the target set ----
        for coin, pos in list(open_pos.items()):
            m = signal.get(coin)
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if m is not None and abs(m) < self.cfg.exit_return:
                reason = f"MOMENTUM-DECAYED ({m*100:+.2f}%)"
            elif want is None:
                reason = "DROPPED from momentum set (rank rotated / decayed)"
            elif want != pos["side"]:
                reason = "MOMENTUM FLIPPED — wrong side now"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"XMOM EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "momentum": m},
                ))

        # ---- entries: fill empty target slots, dollar-neutral per leg ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        for coin, side in desired.items():
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
                    f"XMOM ENTER {direction} {coin} @ ${mid:.4f} "
                    f"{self.cfg.lookback_bars}b-return {m*100:+.2f}%, notional ${notional:.2f}"
                ),
                market_snapshot={"momentum": m, "mid": mid,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(f"no momentum: {len(longs)} long / {len(shorts)} short legs "
                           f"beyond {self.cfg.enter_return*100:.2f}% over {self.cfg.lookback_bars}b"),
                market_snapshot={"n_longs": len(longs), "n_shorts": len(shorts)},
            ))
        return out
