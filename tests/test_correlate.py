"""Daily-PnL correlation helpers (B-EDGE2c).

The diversification claim for a second strategy rests on these numbers, so the
bucketing/alignment math gets exact-value tests: day boundaries, gap days,
baseline handling, and the intersection alignment that keeps a longer warmup
on one arm from skewing the correlation.
"""

from __future__ import annotations

from hl_bot.backtest.correlate import (
    MS_PER_DAY,
    daily_pnl,
    pearson,
    pnl_correlation,
)

H = 3_600_000  # one hour in ms


def test_daily_pnl_buckets_by_utc_day_with_starting_equity():
    curve = [
        (0 * H, 101.0), (12 * H, 103.0), (23 * H, 102.0),          # day 0 closes 102
        (24 * H, 105.0), (47 * H, 99.0),                           # day 1 closes 99
        (48 * H, 110.0),                                           # day 2 closes 110
    ]
    pnl = daily_pnl(curve, starting_equity=100.0)
    assert pnl == {0: 2.0, 1: -3.0, 2: 11.0}
    assert sum(pnl.values()) == 110.0 - 100.0  # days partition total PnL


def test_daily_pnl_without_starting_equity_baselines_on_first_point():
    curve = [(0, 100.0), (12 * H, 104.0), (24 * H, 106.0)]
    pnl = daily_pnl(curve)
    # day 0: close 104 vs first point 100 (the first bar's own PnL is dropped)
    assert pnl == {0: 4.0, 1: 2.0}


def test_daily_pnl_latest_timestamp_wins_within_a_day():
    curve = [(5 * H, 50.0), (2 * H, 200.0)]  # out of order; 5h is the close
    assert daily_pnl(curve, starting_equity=40.0) == {0: 10.0}


def test_daily_pnl_gap_day_attributes_move_to_next_present_day():
    curve = [(0, 100.0), (2 * MS_PER_DAY, 130.0)]  # day 1 absent entirely
    pnl = daily_pnl(curve, starting_equity=90.0)
    assert pnl == {0: 10.0, 2: 30.0}
    assert 1 not in pnl


def test_daily_pnl_empty_curve():
    assert daily_pnl([]) == {}


def test_pearson_perfect_and_inverse():
    assert pearson([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == 1.0
    assert pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0


def test_pearson_undefined_cases():
    assert pearson([1.0, 2.0], [1.0, 2.0]) is None              # <3 points
    assert pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None    # constant arm
    assert pearson([1.0, 2.0, 3.0], [1.0, 2.0]) is None         # length mismatch


def test_pnl_correlation_aligns_on_overlapping_days_only():
    # A spans days 0-3; B starts a day later (longer warmup) and spans 1-3.
    a = [(d * MS_PER_DAY, 100.0 + d) for d in range(4)]
    b = [(d * MS_PER_DAY, 100.0 + 2 * d) for d in range(1, 4)]
    res = pnl_correlation(a, b, starting_a=100.0, starting_b=100.0)
    assert res.days == [1, 2, 3]
    assert res.a == [1.0, 1.0, 1.0]
    # B's day-1 PnL is vs starting equity (its first day), then +2/day.
    assert res.b == [2.0, 2.0, 2.0]
    # Both arms constant → correlation undefined, not a fake 1.0.
    assert res.correlation is None
    assert res.n_days == 3


def test_pnl_correlation_anticorrelated_arms():
    days = range(6)
    moves = [3.0, -2.0, 5.0, -4.0, 1.0, -3.0]
    eq_a, eq_b, a_curve, b_curve = 100.0, 100.0, [], []
    for d, m in zip(days, moves, strict=True):
        eq_a += m
        eq_b -= m
        a_curve.append((d * MS_PER_DAY, eq_a))
        b_curve.append((d * MS_PER_DAY, eq_b))
    res = pnl_correlation(a_curve, b_curve, starting_a=100.0, starting_b=100.0)
    assert res.correlation is not None
    assert abs(res.correlation - (-1.0)) < 1e-9
    assert "corr -1.00 over 6 overlapping days" in res.summary()


def test_pnl_correlation_summary_totals():
    a = [(d * MS_PER_DAY, 100.0 + d) for d in range(4)]
    b = [(d * MS_PER_DAY, 100.0 - d) for d in range(4)]
    res = pnl_correlation(a, b, starting_a=100.0, starting_b=100.0)
    assert sum(res.a) == 3.0
    assert sum(res.b) == -3.0
    assert "(A $+3.00, B $-3.00)" in res.summary()
