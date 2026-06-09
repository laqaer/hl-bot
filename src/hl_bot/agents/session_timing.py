"""Session-Timing — a clock-time / calendar-seasonality thesis.

Thesis (the eighth, *structurally-different* class — the first that keys off
neither price nor funding). Every signal pruned so far — TWAP-MR, funding carry,
cross-sectional momentum (±regime), time-series momentum, majors-1d momentum, and
pairs reversion — is some function of *recent prices or funding levels*, and every
one proved window-specific under the out-of-time durability bar. Session-timing is
orthogonal to that whole family: it keys **only off the bar's UTC clock time**
(hour-of-day + weekday from ``view.ts_ms``) and reads **zero** price/funding input
to form its signal. The a-priori economic hypothesis is the documented crypto
"TradFi-session" effect: liquid majors inherit equity beta, so they realize a
different average drift *during* the US equity cash session (~13:30–20:00 UTC,
weekdays) than *overnight / on weekends* when TradFi is closed. The strategy takes
net-directional LONG exposure **only inside an a-priori-fixed session window** and
is flat outside it; an ``invert`` flag trades the *complement* (the overnight /
weekend window) at no extra code, so both halves of the clock are testable.

This is a *calendar* edge, not a *direction-of-price* edge: the entry/exit times
are deterministic and fixed in advance (no parameter search over hours to "find
the good window" — the US-session window is specified a priori from the TradFi
correlation), which is what keeps it out of the data-mining trap the momentum
leads fell into. Whether a fixed clock window carries net-of-cost edge that
survives a disjoint out-of-time window (``confirm --windows 2+``) is an empirical
question — exactly the bar that pruned the seven price/funding theses. A single
120d window's session drift is partly a bet on that window's regime, so the
multi-window, sign-stable durability test is the honest judge, not a trailing PASS.

Designed for maker execution (confirm with ``--prefer maker``): entries fire on the
deterministic session open and exits on the session close, so they are patient and
poolable across the held coins. No price signal means no per-bar churn inside the
window — one round trip per coin per session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class SessionTimingConfig:
    enter_hour_utc: int = 14                     # US equity open ~13:30 UTC -> 14:00 bar
    exit_hour_utc: int = 21                      # US equity close 20:00 UTC + buffer
    weekdays_only: bool = True                   # TradFi closed on weekends
    invert: bool = False                         # trade the complement (overnight/weekend)
    side: str = "B"                              # "B" long the session, "A" short it
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6


def in_session(
    ts_ms: int,
    enter_hour_utc: int,
    exit_hour_utc: int,
    *,
    weekdays_only: bool = True,
    invert: bool = False,
) -> bool:
    """True iff the bar at ``ts_ms`` falls inside the a-priori session window.

    The window is the UTC-hour band ``[enter_hour_utc, exit_hour_utc)``. If
    ``enter < exit`` it is a same-day intraday band; if ``enter > exit`` it wraps
    midnight. ``weekdays_only`` further restricts the band to Mon–Fri (UTC). The
    final result is negated when ``invert`` is set, so the complement (overnight /
    weekend) is testable from the same code. Pure and unit-testable — no price input.
    """
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
    hour = dt.hour
    if enter_hour_utc <= exit_hour_utc:
        within_hours = enter_hour_utc <= hour < exit_hour_utc
    else:  # window wraps midnight
        within_hours = hour >= enter_hour_utc or hour < exit_hour_utc
    is_weekday = dt.weekday() < 5
    inside = within_hours and (is_weekday or not weekdays_only)
    return (not inside) if invert else inside


class SessionTimingAgent(Agent):
    def __init__(
        self,
        name: str = "session_timing_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = SessionTimingConfig(
            enter_hour_utc=int(c.get("enter_hour_utc", 14)),
            exit_hour_utc=int(c.get("exit_hour_utc", 21)),
            weekdays_only=bool(c.get("weekdays_only", True)),
            invert=bool(c.get("invert", False)),
            side=str(c.get("side", "B")),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
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

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        active = in_session(
            view.ts_ms, self.cfg.enter_hour_utc, self.cfg.exit_hour_utc,
            weekdays_only=self.cfg.weekdays_only, invert=self.cfg.invert,
        )

        # eligible universe: liquid coins with a valid mid (no price signal used)
        eligible = [
            coin for coin, mid in view.mids.items()
            if (mid or 0) > 0 and vol.get(coin, 0) >= self.cfg.min_daily_volume_usd
        ]

        # ---- exits: session closed -> flatten everything we hold ----
        if not active:
            for coin, pos in list(open_pos.items()):
                mid = view.mids.get(coin)
                if mid is None or mid <= 0:
                    continue
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"SESSION CLOSED — flatten {coin} @ ${mid:.4f}",
                    market_snapshot={"exit_px": mid, "in_session": False},
                ))
            if not out:
                out.append(Decision(
                    agent=self.name, action="hold",
                    reasoning="outside session window — flat",
                    market_snapshot={"in_session": False},
                ))
            return out

        # ---- entries: session open -> hold the eligible universe up to caps ----
        flattening: set[str] = set()
        active_after = set(open_pos.keys()) - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"]) for c, p in open_pos.items()
        )
        room_notional = self.cfg.max_total_notional - active_notional

        for coin in sorted(eligible, key=lambda c: -vol.get(c, 0)):
            if room <= 0 or room_notional < 5.0:
                break
            if coin in active_after:
                continue
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            sz = round(notional / mid, 5)
            direction = "short" if self.cfg.side == "A" else "long"
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=self.cfg.side, sz=sz, px=mid,
                cloid=make_cloid(self.name),
                reasoning=(
                    f"SESSION OPEN — {direction} {coin} @ ${mid:.4f} "
                    f"(hours {self.cfg.enter_hour_utc:02d}-{self.cfg.exit_hour_utc:02d}Z"
                    f"{', weekdays' if self.cfg.weekdays_only else ''}"
                    f"{', inverted' if self.cfg.invert else ''}), notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "notional": notional,
                                 "leg": direction, "in_session": True},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning="in session but no eligible coin / caps full",
                market_snapshot={"in_session": True, "eligible": len(eligible)},
            ))
        return out
