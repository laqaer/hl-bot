"""Prop-firm evaluation rules as code (CAPITAL.md Track B, backlog B-PROP).

A funded-account eval is a risk contract: trade a sim account for ~5-10 days
and a single breach of the firm's rules forfeits the eval fee (and, once
funded, the account). The rules differ from the bot's own guardrails in three
ways that matter:

- they are measured on **equity** (account value *including* unrealized PnL),
  not realized fills — an intraday mark-to-market dip below the line fails
  you even if the position later closes green;
- the daily loss limit resets at a fixed **day boundary**, not a rolling 24h
  window (``exec.orders.check_guardrails`` is rolling + realized-only);
- max drawdown is anchored to the equity **high-water mark** (trailing) or
  the starting balance (static), forever — not a 7d window.

This module replays an equity curve (live ``equity_snapshots``, or any
(ts_ms, equity) series, e.g. a backtest equity curve) against an
:class:`EvalProfile` and reports every would-be breach, the current headroom
to each line, and a PASS/FAIL/IN-PROGRESS verdict. Pure functions, no
network; ``hlbot prop-check`` is the read-only CLI.

Honesty caveats (also printed by the CLI):

- Snapshots are sampled. A breach *between* observations is invisible here
  but fatal in a real eval, which marks continuously — treat a thin margin
  as a fail, and prefer dense snapshots (the report includes observation
  density and the largest gap).
- The shipped default numbers are PLACEHOLDERS shaped like common prop
  rules. Verify the actual firm's current terms (docs/PROP_EVAL.md) and pass
  them explicitly before relying on a verdict.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class EvalProfile:
    """One firm's eval rules. Every figure must be verified against the
    firm's current terms before a verdict is trusted (they change)."""

    name: str
    start_balance: float
    # Daily loss: equity may not drop more than this fraction below the
    # day-open equity. ``daily_loss_base`` picks what the fraction is *of*:
    # "start" = fixed dollars (pct x start_balance, FTMO-style),
    # "day_open" = pct x that day's opening equity.
    max_daily_loss_pct: float
    daily_loss_base: Literal["start", "day_open"] = "start"
    # Max drawdown: "trailing" anchors to the running equity high-water mark,
    # "static" to start_balance. Trailing is the stricter and the more common
    # rule at crypto-native firms.
    max_drawdown_pct: float = 0.10
    drawdown_mode: Literal["trailing", "static"] = "trailing"
    # Pass conditions (0 disables a condition).
    profit_target_pct: float = 0.0
    min_trading_days: int = 0
    # Hour (UTC) at which the daily limit resets.
    day_boundary_utc_hour: int = 0


@dataclass(frozen=True)
class BreachEvent:
    ts_ms: int
    rule: Literal["daily_loss", "max_drawdown"]
    equity: float
    floor: float
    detail: str


@dataclass
class EvalReport:
    profile: EvalProfile
    n_points: int = 0
    first_ts_ms: int | None = None
    last_ts_ms: int | None = None
    breaches: list[BreachEvent] = field(default_factory=list)
    # First time equity touched start_balance x (1 + profit_target_pct).
    target_reached_ts_ms: int | None = None
    trading_days: int | None = None  # None = caller supplied no fill data
    # State at the last observation.
    last_equity: float | None = None
    high_water_mark: float | None = None
    day_open_equity: float | None = None
    daily_floor: float | None = None
    drawdown_floor: float | None = None
    # Observation density (sampled-curve honesty).
    obs_per_day: float | None = None
    max_gap_hours: float | None = None

    @property
    def verdict(self) -> str:
        """FAIL on any breach; PASS only when every configured pass condition
        is met breach-free; otherwise IN_PROGRESS. Conservative: a breach
        after the target date still reads FAIL, because a funded account
        lives under the same rules the breach just violated."""
        if self.n_points == 0:
            return "NO_DATA"
        if self.breaches:
            return "FAIL"
        p = self.profile
        if p.profit_target_pct > 0 and self.target_reached_ts_ms is None:
            return "IN_PROGRESS"
        if p.min_trading_days > 0 and (self.trading_days or 0) < p.min_trading_days:
            return "IN_PROGRESS"
        return "PASS"

    @property
    def daily_headroom(self) -> float | None:
        if self.last_equity is None or self.daily_floor is None:
            return None
        return self.last_equity - self.daily_floor

    @property
    def drawdown_headroom(self) -> float | None:
        if self.last_equity is None or self.drawdown_floor is None:
            return None
        return self.last_equity - self.drawdown_floor


def _day_index(ts_ms: int, boundary_hour: int) -> int:
    return (ts_ms - boundary_hour * HOUR_MS) // DAY_MS


def _daily_allowance(p: EvalProfile, day_open: float) -> float:
    base = p.start_balance if p.daily_loss_base == "start" else day_open
    return p.max_daily_loss_pct * base


def simulate_eval(
    profile: EvalProfile,
    points: list[tuple[int, float]],
    trading_days: set[int] | None = None,
) -> EvalReport:
    """Replay an equity curve against the eval rules.

    ``points`` are (ts_ms, equity) observations, any order, equity inclusive
    of unrealized PnL (HL ``accountValue`` qualifies). ``trading_days`` is an
    optional set of day indices (``trading_day_index``) on which at least one
    trade happened, for the min-trading-days pass condition.

    Day-open equity for a day is the equity carried at the boundary — i.e.
    the last observation of the previous day (the first observation overall
    for the first day). Breaches are collapsed into episodes: one event per
    day per rule for the daily line, one per excursion below the line for the
    drawdown rule.
    """
    report = EvalReport(profile=profile, trading_days=(
        len(trading_days) if trading_days is not None else None))
    if not points:
        return report
    pts = sorted(points)
    report.n_points = len(pts)
    report.first_ts_ms, report.last_ts_ms = pts[0][0], pts[-1][0]

    boundary = profile.day_boundary_utc_hour
    hwm = profile.start_balance
    target = (
        profile.start_balance * (1 + profile.profit_target_pct)
        if profile.profit_target_pct > 0 else None
    )
    static_floor = profile.start_balance * (1 - profile.max_drawdown_pct)

    day = _day_index(pts[0][0], boundary)
    day_open = last_eq = pts[0][1]
    daily_breached_days: set[int] = set()
    dd_in_breach = False
    prev_ts: int | None = None
    max_gap = 0.0

    for ts, eq in pts:
        if prev_ts is not None:
            max_gap = max(max_gap, (ts - prev_ts) / HOUR_MS)
        prev_ts = ts

        d = _day_index(ts, boundary)
        if d != day:
            day = d
            day_open = last_eq  # equity carried over the boundary
        last_eq = eq

        hwm = max(hwm, eq)
        if target is not None and report.target_reached_ts_ms is None and eq >= target:
            report.target_reached_ts_ms = ts

        daily_floor = day_open - _daily_allowance(profile, day_open)
        if eq <= daily_floor and d not in daily_breached_days:
            daily_breached_days.add(d)
            report.breaches.append(BreachEvent(
                ts, "daily_loss", eq, daily_floor,
                f"equity {eq:.2f} <= day floor {daily_floor:.2f} "
                f"(day open {day_open:.2f})"))

        dd_floor = (
            hwm * (1 - profile.max_drawdown_pct)
            if profile.drawdown_mode == "trailing" else static_floor
        )
        if eq <= dd_floor:
            if not dd_in_breach:
                dd_in_breach = True
                report.breaches.append(BreachEvent(
                    ts, "max_drawdown", eq, dd_floor,
                    f"equity {eq:.2f} <= {profile.drawdown_mode} DD floor "
                    f"{dd_floor:.2f} (HWM {hwm:.2f})"))
        else:
            dd_in_breach = False

    report.breaches.sort(key=lambda b: b.ts_ms)
    report.last_equity = last_eq
    report.high_water_mark = hwm
    report.day_open_equity = day_open
    report.daily_floor = day_open - _daily_allowance(profile, day_open)
    report.drawdown_floor = (
        hwm * (1 - profile.max_drawdown_pct)
        if profile.drawdown_mode == "trailing" else static_floor
    )
    span_days = (report.last_ts_ms - report.first_ts_ms) / DAY_MS
    report.obs_per_day = len(pts) / span_days if span_days > 0 else None
    report.max_gap_hours = max_gap if len(pts) > 1 else None
    return report


def trading_day_index(ts_ms: int, boundary_hour: int = 0) -> int:
    """Public day-index helper so callers bucket fills the same way the
    simulator buckets equity."""
    return _day_index(ts_ms, boundary_hour)


def equity_points(
    conn: sqlite3.Connection, since_ms: int = 0
) -> list[tuple[int, float]]:
    """(ts_ms, account_value) from equity_snapshots, oldest first."""
    rows = conn.execute(
        "SELECT ts_ms, account_value FROM equity_snapshots "
        "WHERE ts_ms >= ? ORDER BY ts_ms", (since_ms,)).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def fill_trading_days(
    conn: sqlite3.Connection, boundary_hour: int = 0, since_ms: int = 0
) -> set[int]:
    """Day indices with at least one fill (any agent — the firm sees the
    account, not our attribution)."""
    rows = conn.execute(
        "SELECT DISTINCT time_ms FROM fills WHERE time_ms >= ?", (since_ms,))
    return {_day_index(int(r[0]), boundary_hour) for r in rows}
