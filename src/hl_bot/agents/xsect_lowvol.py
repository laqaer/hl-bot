"""X-Sectional Low-Volatility — betting-against-volatility (the eleventh thesis).

Thesis (the structurally-different signal after ten theses were pruned). Every
candidate searched so far keyed off price *return* (TWAP-MR, cross-sectional and
time-series momentum, majors-1d momentum), *funding level* (carry), a *pairwise
price ratio* (pairs), a *cross-market gap* (basis), the *clock* (session), or
*microstructure* (maker spread). None keyed off the **cross-section of realized
volatility**. The low-volatility anomaly (Frazzini-Pedersen "betting against
beta") is one of the most robust factors in TradFi equities, FX, and commodities:
leverage-constrained investors overpay for high-risk assets, so low-risk names
deliver *higher risk-adjusted* returns. The dollar-neutral expression is LONG the
calmest coins / SHORT the most volatile coins.

Why it might behave differently under the durability bar that pruned the return
ranks: realized volatility is far more *persistent* (autocorrelated) bar-to-bar
than realized return, so the rank rotates slowly and is less of a bet on one
window's return regime — exactly the regime-sensitivity that sign-flipped the
momentum leads across disjoint windows. Whether that persistence translates into
a net-of-cost, cross-window-durable edge is an empirical question for
``confirm --windows 2+ --prefer maker``, not a guess.

A single ``invert`` flag flips the legs (LONG high-vol / SHORT low-vol) so the
*same* book tests the opposite "lottery-demand / high-vol-chase" thesis at no
extra code. Designed for maker execution: the slow-rotating rank means patient
entries. Exit a held coin when it leaves the target set (vol rank rotated) or
switches legs.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class XSectLowVolConfig:
    vol_lookback: int = 48                       # bars of returns for realized vol
    top_k: int = 2                               # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    invert: bool = False                         # True: long high-vol / short low-vol


class XSectLowVolAgent(Agent):
    def __init__(
        self,
        name: str = "xsect_lowvol_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = XSectLowVolConfig(
            vol_lookback=int(c.get("vol_lookback", 48)),
            top_k=int(c.get("top_k", 2)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
            invert=bool(c.get("invert", False)),
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
    def _realized_vol(closes: list[float], lb: int) -> float | None:
        """Sample std of the last ``lb`` log-returns. None if too short / bad prices."""
        if not closes or len(closes) < lb + 1:
            return None
        window = closes[-(lb + 1):]
        rets: list[float] = []
        for prev, cur in zip(window, window[1:], strict=False):
            if prev <= 0 or cur <= 0:
                return None
            rets.append(math.log(cur / prev))
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- per-coin realized-volatility signal ----
        rvol: dict[str, float] = {}
        for coin, series in closes.items():
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            v = self._realized_vol(series, self.cfg.vol_lookback)
            if v is not None:
                rvol[coin] = v

        # ---- rank ascending by vol; long the calmest, short the wildest ----
        ranked = sorted(rvol.items(), key=lambda kv: kv[1])
        if len(ranked) >= 2 * self.cfg.top_k:
            low_vol = [c for c, _ in ranked[: self.cfg.top_k]]    # calmest -> long
            high_vol = [c for c, _ in ranked[-self.cfg.top_k:]]   # wildest -> short
        else:
            low_vol, high_vol = [], []
        if self.cfg.invert:
            low_vol, high_vol = high_vol, low_vol
        desired: dict[str, str] = {c: "B" for c in low_vol}
        desired.update({c: "A" for c in high_vol})

        # ---- exits: leave the book when a coin drops out / switches legs ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if want is None:
                reason = "DROPPED from vol-rank set (rank rotated)"
            elif want != pos["side"]:
                reason = "VOL RANK FLIPPED — wrong leg now"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"LOWVOL EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "rvol": rvol.get(coin)},
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
            v = rvol.get(coin)
            if not mid or v is None:
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
                    f"LOWVOL ENTER {direction} {coin} @ ${mid:.4f} "
                    f"{self.cfg.vol_lookback}b-rvol {v*100:.2f}%, notional ${notional:.2f}"
                ),
                market_snapshot={"rvol": v, "mid": mid,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no vol-rank book: {len(low_vol)} long / {len(high_vol)} short legs "
                    f"({len(rvol)} eligible, need {2 * self.cfg.top_k})"
                ),
                market_snapshot={"n_longs": len(low_vol), "n_shorts": len(high_vol),
                                 "n_eligible": len(rvol)},
            ))
        return out
