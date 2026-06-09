"""X-Sectional return-skewness / lottery-demand — the MAX effect (the THIRTEENTH thesis).

Thesis (the structurally-different signal after twelve theses were pruned). Every
candidate searched so far keyed off the **first or second moment** of the return
distribution — price *return* itself (TWAP-MR, cross-sectional / time-series /
majors-1d momentum), its *variance* (low-vol, the eleventh thesis) — or off
orthogonal axes: *funding level* (carry), a *pairwise ratio* (pairs), a
*cross-market gap* (basis), the *clock* (session), *microstructure* (maker spread),
or *liquidity / volume* (illiq, the twelfth). None keyed off the **third moment**:
the cross-section of return *skewness*.

The lottery-demand / MAX anomaly (Bali, Cakici & Whitelaw 2011, "Maximum daily
returns and the cross-section of expected returns") is one of TradFi's most robust
a-priori factors: investors over-pay for positively-skewed, lottery-like payoffs
(a small chance of a large up-move), so high-skew names are *over*-priced and
subsequently *under*-perform. The dollar-neutral expression is therefore SHORT the
highest-skew coins / LONG the lowest-skew coins. Crypto perps are, a-priori, an
even stronger habitat for lottery demand than equities (retail-heavy, meme-driven,
convex narratives), so the effect — if it exists net of maker cost — should be at
least as pronounced here.

Why it might behave differently under the durability bar that pruned the return
ranks: skewness is a *distributional shape* statistic, not a directional return
bet, so — like realized vol (low-vol) and liquidity (illiq) — its rank rotates on
the persistence of *who is lottery-like*, not on which window's return regime won.
That is exactly the regime-sensitivity that sign-flipped every momentum lead across
disjoint windows. Whether the persistence translates into a net-of-cost,
cross-window-durable edge is an empirical question for
``confirm --windows 2+ --prefer maker``, not a guess.

Two ``signal`` forms express the same lottery-demand idea, mirroring the illiq
decomposition (``amihud|volume|absret``) so the driver can be attributed rather
than asserted:
  * ``max``  — the canonical MAX statistic: the mean of the ``n_max`` largest
    bar log-returns over ``skew_lookback`` (the size of the recent lottery upside).
  * ``skew`` — the sample (third-standardized-moment) skewness of the bar
    log-returns over ``skew_lookback`` (the whole-distribution asymmetry).
A single ``invert`` flag flips the legs (LONG high-skew / SHORT low-skew) so the
*same* book tests the opposite "skewness-momentum / chase-the-lottery" thesis at no
extra code. Designed for maker execution: the slow-rotating rank means patient
entries. Exit a held coin when it leaves the target set (rank rotated) or switches
legs.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision

_SIGNALS = frozenset({"max", "skew"})


@dataclass
class XSectSkewConfig:
    skew_lookback: int = 48                       # bars of returns for the shape stat
    n_max: int = 5                                # MAX: how many top returns to average
    signal: str = "max"                           # "max" | "skew"
    top_k: int = 2                                # legs per side
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6
    invert: bool = False                          # True: long high-skew / short low-skew


class XSectSkewAgent(Agent):
    def __init__(
        self,
        name: str = "xsect_skew_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        signal = str(c.get("signal", "max"))
        if signal not in _SIGNALS:
            raise ValueError(f"signal must be one of {sorted(_SIGNALS)}, got {signal!r}")
        self.cfg = XSectSkewConfig(
            skew_lookback=int(c.get("skew_lookback", 48)),
            n_max=int(c.get("n_max", 5)),
            signal=signal,
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
    def _log_returns(closes: list[float], lb: int) -> list[float] | None:
        """The last ``lb`` bar log-returns. None if too short / non-positive prices."""
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
        return rets

    @staticmethod
    def _lottery_signal(
        closes: list[float], lb: int, signal: str = "max", n_max: int = 5
    ) -> float | None:
        """Lottery-demand statistic over the last ``lb`` bar log-returns.

        ``max``  -> mean of the ``n_max`` largest returns (the MAX effect).
        ``skew`` -> sample skewness (third standardized moment) of the returns.
        Higher = more lottery-like in both forms, so the ranking is sign-consistent
        (SHORT the highest, LONG the lowest). None if the window is too short, has a
        non-positive price, or is degenerate (zero dispersion for ``skew``).
        """
        rets = XSectSkewAgent._log_returns(closes, lb)
        if rets is None:
            return None
        if signal == "max":
            k = max(1, min(n_max, len(rets)))
            top = sorted(rets, reverse=True)[:k]
            return sum(top) / len(top)
        if signal == "skew":
            n = len(rets)
            mean = sum(rets) / n
            m2 = sum((r - mean) ** 2 for r in rets) / n
            m3 = sum((r - mean) ** 3 for r in rets) / n
            sd = math.sqrt(m2)
            if sd <= 0:
                return None
            return m3 / sd ** 3
        raise ValueError(f"unknown signal {signal!r}")

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- per-coin lottery-demand signal ----
        skew: dict[str, float] = {}
        for coin, series in closes.items():
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            if (view.mids.get(coin) or 0) <= 0:
                continue
            s = self._lottery_signal(
                series, self.cfg.skew_lookback, self.cfg.signal, self.cfg.n_max
            )
            if s is not None:
                skew[coin] = s

        # ---- rank ascending; long the least lottery-like, short the most ----
        ranked = sorted(skew.items(), key=lambda kv: kv[1])
        if len(ranked) >= 2 * self.cfg.top_k:
            low_skew = [c for c, _ in ranked[: self.cfg.top_k]]    # least lottery -> long
            high_skew = [c for c, _ in ranked[-self.cfg.top_k:]]   # most lottery -> short
        else:
            low_skew, high_skew = [], []
        if self.cfg.invert:
            low_skew, high_skew = high_skew, low_skew
        desired: dict[str, str] = {c: "B" for c in low_skew}
        desired.update({c: "A" for c in high_skew})

        # ---- exits: leave the book when a coin drops out / switches legs ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            want = desired.get(coin)
            reason = None
            if want is None:
                reason = "DROPPED from skew-rank set (rank rotated)"
            elif want != pos["side"]:
                reason = "SKEW RANK FLIPPED — wrong leg now"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"SKEW EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "skew": skew.get(coin)},
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
            s = skew.get(coin)
            if not mid or s is None:
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
                    f"SKEW ENTER {direction} {coin} @ ${mid:.4f} "
                    f"{self.cfg.skew_lookback}b-{self.cfg.signal} {s:.4f}, "
                    f"notional ${notional:.2f}"
                ),
                market_snapshot={"skew": s, "mid": mid,
                                 "notional": notional, "leg": direction},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no skew-rank book: {len(low_skew)} long / {len(high_skew)} short legs "
                    f"({len(skew)} eligible, need {2 * self.cfg.top_k})"
                ),
                market_snapshot={"n_longs": len(low_skew), "n_shorts": len(high_skew),
                                 "n_eligible": len(skew)},
            ))
        return out
