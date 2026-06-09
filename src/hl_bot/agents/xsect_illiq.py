"""X-Sectional Illiquidity — the Amihud illiquidity premium (the twelfth thesis).

Thesis (the structurally-different signal after eleven theses were pruned). Every
candidate searched so far keyed off price *return* (TWAP-MR, cross-sectional and
time-series momentum, majors-1d momentum), *funding level* (carry), a *pairwise
price ratio* (pairs), a *cross-market gap* (basis), the *clock* (session),
*microstructure* (maker spread), or the *cross-section of realized volatility*
(low-vol). None keyed off **liquidity / trading volume**. The Amihud (2002)
illiquidity premium is — alongside size, value, momentum, and low-volatility —
one of the most robust a-priori cross-asset factors: less-liquid assets must
compensate holders for price-impact / inventory risk, so they earn higher
expected returns. The dollar-neutral expression is LONG the most-illiquid coins /
SHORT the most-liquid coins.

Illiquidity is measured Amihud-style as **price impact per dollar of volume** —
the average absolute return over ``illiq_lookback`` bars divided by the coin's
dollar volume. A coin that moves a lot on little volume is illiquid (high
measure); a coin that absorbs large volume with small moves is liquid (low
measure). This keys off the volume the cached frames already carry
(``day_ntl_vlm`` as the dollar-volume normalizer, ``closes`` for the |returns|),
so no new data plumbing is needed.

Why it might behave differently under the durability bar that pruned the return
ranks and the low-vol rank: liquidity (dollar volume) is even more *persistent*
bar-to-bar than realized volatility — a coin's relative thickness barely rotates
window-to-window — so the rank is a slow, structural tilt rather than a bet on
one window's return regime, the exact regime-sensitivity that sign-flipped the
momentum leads. Whether that persistence translates into a net-of-cost,
cross-window-durable edge is an empirical question for
``confirm --windows 2+ --prefer maker``, not a guess.

A single ``invert`` flag flips the legs (LONG liquid / SHORT illiquid) so the
*same* book tests the opposite "liquidity-chase" thesis at no extra code.
Designed for maker execution: the slow-rotating rank means patient entries. Exit
a held coin when it leaves the target set (rank rotated) or switches legs.
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
class XSectIlliqConfig:
    illiq_lookback: int = 48                      # bars of returns for the |r| average
    top_k: int = 2                               # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    invert: bool = False                         # True: long liquid / short illiquid


class XSectIlliqAgent(Agent):
    def __init__(
        self,
        name: str = "xsect_illiq_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = XSectIlliqConfig(
            illiq_lookback=int(c.get("illiq_lookback", 48)),
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
    def _illiquidity(closes: list[float], dollar_vol: float, lb: int) -> float | None:
        """Amihud-style price impact per dollar of volume.

        Mean absolute log-return over the last ``lb`` bars divided by the coin's
        dollar volume. ``None`` if the series is too short, prices are bad, or the
        dollar volume is non-positive (can't normalize).
        """
        if dollar_vol <= 0 or not closes or len(closes) < lb + 1:
            return None
        window = closes[-(lb + 1):]
        abs_rets: list[float] = []
        for prev, cur in zip(window, window[1:], strict=False):
            if prev <= 0 or cur <= 0:
                return None
            abs_rets.append(abs(math.log(cur / prev)))
        if not abs_rets:
            return None
        return (sum(abs_rets) / len(abs_rets)) / dollar_vol

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- per-coin Amihud illiquidity signal ----
        illiq: dict[str, float] = {}
        for coin, series in closes.items():
            dollar_vol = vol.get(coin, 0)
            if dollar_vol < self.cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            il = self._illiquidity(series, dollar_vol, self.cfg.illiq_lookback)
            if il is not None:
                illiq[coin] = il

        # ---- rank ascending; long the most-illiquid, short the most-liquid ----
        ranked = sorted(illiq.items(), key=lambda kv: kv[1])
        if len(ranked) >= 2 * self.cfg.top_k:
            liquid = [c for c, _ in ranked[: self.cfg.top_k]]      # most liquid -> short
            illiquid = [c for c, _ in ranked[-self.cfg.top_k:]]    # most illiquid -> long
        else:
            liquid, illiquid = [], []
        long_legs, short_legs = illiquid, liquid
        if self.cfg.invert:
            long_legs, short_legs = short_legs, long_legs
        desired: dict[str, str] = {c: "B" for c in long_legs}
        desired.update({c: "A" for c in short_legs})

        # ---- exits: leave the book when a coin drops out / switches legs ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if want is None:
                reason = "DROPPED from illiq-rank set (rank rotated)"
            elif want != pos["side"]:
                reason = "ILLIQ RANK FLIPPED — wrong leg now"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"ILLIQ EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "illiq": illiq.get(coin)},
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
            il = illiq.get(coin)
            if not mid or il is None:
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
                    f"ILLIQ ENTER {direction} {coin} @ ${mid:.4f} "
                    f"{self.cfg.illiq_lookback}b-illiq {il:.3e}, notional ${notional:.2f}"
                ),
                market_snapshot={"illiq": il, "mid": mid,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no illiq-rank book: {len(long_legs)} long / {len(short_legs)} short legs "
                    f"({len(illiq)} eligible, need {2 * self.cfg.top_k})"
                ),
                market_snapshot={"n_longs": len(long_legs), "n_shorts": len(short_legs),
                                 "n_eligible": len(illiq)},
            ))
        return out
