"""Equity-curve statistics shared by live scoring and the backtest engine.

Moved out of ``backtest.engine`` so production scoring (`scoring.metrics`,
`reports.track_record`) can compute per-agent Sharpe/drawdown with the *same*
math the backtester uses — sim and live can never silently disagree.
"""

from __future__ import annotations


def curve_stats(
    curve: list[tuple[int, float]],
    periods_per_year: float = 365 * 24,
) -> tuple[float | None, float | None, float | None]:
    """Sharpe / max-drawdown / Calmar from an equity curve.

    ``periods_per_year`` defaults to hourly bars; pass the right cadence for
    other intervals. Returns (None, None, None) when there isn't enough data.
    """
    if len(curve) < 3:
        return None, None, None
    eq = [v for _, v in curve]
    rets = [
        (eq[i] - eq[i - 1]) / eq[i - 1]
        for i in range(1, len(eq))
        if eq[i - 1] != 0
    ]
    sharpe = None
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (periods_per_year ** 0.5)
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v - peak) / peak)
    dd = max_dd if max_dd < 0 else 0.0
    calmar = None
    if dd < 0 and rets:
        ann_ret = (1 + sum(rets) / len(rets)) ** periods_per_year - 1
        calmar = ann_ret / abs(dd)
    return sharpe, dd, calmar


def dollar_max_drawdown(curve: list[tuple[int, float]]) -> float | None:
    """Largest peak-to-trough *dollar* drop of a cumulative-PnL curve.

    Scale-free percentage drawdown is undefined for an agent without its own
    capital base, so per-agent gates use this dollar figure instead. Returns a
    negative number (or 0.0 when the curve never draws down); None if empty.
    """
    if not curve:
        return None
    peak = curve[0][1]
    worst = 0.0
    for _, v in curve:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst
