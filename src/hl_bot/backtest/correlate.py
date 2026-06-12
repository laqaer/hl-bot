"""Daily-PnL correlation between two backtest equity curves (B-EDGE2c).

A second strategy only diversifies the book if its PnL is genuinely
low-correlated with the first — "different signal family" is a thesis, not
evidence. These helpers turn two equity curves from the backtest engine into
aligned UTC-day PnL series and a Pearson correlation, so the diversification
claim gets a number produced by the same engine as the edge claims.

Day buckets are UTC calendar days (``ts_ms // MS_PER_DAY``); a day's PnL is
the change in the day's *last* equity vs the previous bucketed day's last
equity, so intraday bar count / cadence differences between the two runs
don't matter — only the daily outcome does. Flat days (no position, zero
PnL) are kept: "A trades while B sits out" is exactly the diversification
being measured, and dropping those days would overstate co-movement.
"""

from __future__ import annotations

from dataclasses import dataclass

MS_PER_DAY = 86_400_000


def daily_pnl(
    curve: list[tuple[int, float]], starting_equity: float | None = None
) -> dict[int, float]:
    """Bucket an equity curve ``[(ts_ms, equity)]`` into UTC-day PnL.

    The baseline for the first day is ``starting_equity`` (pass the run's
    starting capital so the first bar's PnL is counted); when omitted, the
    first point's equity is used, silently dropping whatever the first bar
    made. Points within a day may arrive in any order; the latest timestamp
    wins as that day's closing equity.
    """
    if not curve:
        return {}
    last_by_day: dict[int, tuple[int, float]] = {}
    for ts_ms, equity in curve:
        day = ts_ms // MS_PER_DAY
        prev = last_by_day.get(day)
        if prev is None or ts_ms >= prev[0]:
            last_by_day[day] = (ts_ms, equity)

    out: dict[int, float] = {}
    prev_equity = (
        min(curve)[1] if starting_equity is None else starting_equity
    )
    for day in sorted(last_by_day):
        equity = last_by_day[day][1]
        out[day] = equity - prev_equity
        prev_equity = equity
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; ``None`` when undefined (<3 points or a constant
    series — a strategy that never traded has no co-movement to measure)."""
    n = len(xs)
    if n != len(ys) or n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mx) ** 2 for x in xs)
    var_y = sum((y - my) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return cov / (var_x * var_y) ** 0.5


@dataclass(frozen=True)
class CorrelationResult:
    """Aligned daily-PnL series for two runs plus their Pearson correlation."""

    days: list[int]          # UTC day indices present in BOTH curves
    a: list[float]           # arm A's PnL on those days
    b: list[float]           # arm B's PnL on those days
    correlation: float | None

    @property
    def n_days(self) -> int:
        return len(self.days)

    def summary(self) -> str:
        corr = "—" if self.correlation is None else f"{self.correlation:+.2f}"
        return (
            f"daily-PnL corr {corr} over {self.n_days} overlapping days "
            f"(A ${sum(self.a):+.2f}, B ${sum(self.b):+.2f})"
        )


def pnl_correlation(
    curve_a: list[tuple[int, float]],
    curve_b: list[tuple[int, float]],
    *,
    starting_a: float | None = None,
    starting_b: float | None = None,
) -> CorrelationResult:
    """Daily-PnL Pearson correlation between two equity curves.

    Only days present in both curves are compared, so a longer warmup or a
    shorter sample on one side shrinks the overlap rather than skewing the
    correlation with unmatched days.
    """
    pnl_a = daily_pnl(curve_a, starting_a)
    pnl_b = daily_pnl(curve_b, starting_b)
    days = sorted(set(pnl_a) & set(pnl_b))
    xs = [pnl_a[d] for d in days]
    ys = [pnl_b[d] for d in days]
    return CorrelationResult(days=days, a=xs, b=ys, correlation=pearson(xs, ys))
