"""Perp-vs-spot basis reversion model — the tenth thesis (basis / term-structure).

The model trades the perp on the rolling z-score of its basis (perp/spot − 1):
short when rich, long when cheap, exit on reversion. These tests pin the
no-lookahead z-score rule, the long/short direction, and the perp-move − fee
decomposition on hand-computable bars.
"""

from __future__ import annotations

from hl_bot.backtest.basis_reversion import (
    BasisBar,
    bars_from_candles,
    simulate_basis_reversion,
    simulate_universe_basis,
)


def _bars(pairs: list[tuple[float, float]]) -> list[BasisBar]:
    return [BasisBar(perp=p, spot=s) for p, s in pairs]


def test_basis_property():
    assert abs(BasisBar(perp=101.0, spot=100.0).basis - 0.01) < 1e-9
    assert BasisBar(perp=100.0, spot=0.0).basis == 0.0  # guard div-by-zero


def test_align_by_open_time_inner_join():
    perp = [
        {"t": 1, "c": 101.0}, {"t": 2, "c": 102.0}, {"t": 3, "c": 103.0},
    ]
    spot = [
        {"t": 1, "c": 100.0}, {"t": 3, "c": 100.0}, {"t": 4, "c": 100.0},
    ]
    bars = bars_from_candles(perp, spot)
    # only t=1 and t=3 are present in both
    assert len(bars) == 2
    assert abs(bars[0].basis - 0.01) < 1e-9
    assert abs(bars[1].basis - 0.03) < 1e-9


def test_flat_basis_never_trades():
    # basis identically zero -> z is 0 every bar -> no entry.
    res = simulate_basis_reversion(
        _bars([(100.0, 100.0)] * 50), lookback_bars=10, entry_z=2.0
    )
    assert res.n_trades == 0
    assert res.net_edge_bps == 0.0
    assert res.net_per_bar_bps == 0.0


def test_warmup_counts_only_eligible_bars():
    res = simulate_basis_reversion(
        _bars([(100.0, 100.0)] * 20), lookback_bars=10, entry_z=2.0
    )
    assert res.n_bars == 20 - (10 - 1)  # bars from index lookback-1 onward


def test_rich_basis_opens_short_and_books_perp_move():
    # 12 calm bars (basis 0) to seed the window, then bar 13 spikes the basis rich
    # (perp 110 vs spot 100 -> basis 0.10, a huge z) -> SHORT the perp at 110.
    # Next bar the basis reverts to 0 (perp 99 vs spot 99) -> exit at 99.
    # SHORT pnl = -1 * (99/110 - 1) = +10% = +1000bps gross.
    seed = [(100.0, 100.0)] * 12
    bars = _bars(seed + [(110.0, 100.0), (99.0, 99.0)])
    res = simulate_basis_reversion(
        bars, lookback_bars=12, entry_z=1.5, exit_z=0.5, maker_fee_bps=1.0
    )
    assert res.n_trades == 1
    assert res.n_short == 1
    assert res.n_long == 0
    assert abs(res.gross_bps - 1000.0) < 1.0
    assert abs(res.net_edge_bps - (res.gross_bps - 2.0)) < 1e-9  # 2x maker fee
    assert res.win_rate == 1.0


def test_cheap_basis_opens_long():
    # mirror: bar spikes the basis cheap (perp 90 vs spot 100) -> LONG at 90,
    # revert to par (perp 100 vs spot 100) -> exit at 100.
    # LONG pnl = +1 * (100/90 - 1) = +11.1% gross.
    seed = [(100.0, 100.0)] * 12
    bars = _bars(seed + [(90.0, 100.0), (100.0, 100.0)])
    res = simulate_basis_reversion(
        bars, lookback_bars=12, entry_z=1.5, exit_z=0.5, maker_fee_bps=0.0
    )
    assert res.n_trades == 1
    assert res.n_long == 1
    assert res.n_short == 0
    assert res.gross_bps > 0


def test_unclosed_position_not_booked():
    # Opens short on the rich spike but the basis never reverts inside the exit
    # band before the series ends -> the position is reported unclosed, not booked.
    seed = [(100.0, 100.0)] * 12
    bars = _bars(seed + [(110.0, 100.0), (111.0, 100.0), (112.0, 100.0)])
    res = simulate_basis_reversion(
        bars, lookback_bars=12, entry_z=1.5, exit_z=0.1
    )
    assert res.n_trades == 0
    assert res.unclosed == 1


def test_universe_pools_trades_equal_weight():
    seed = [(100.0, 100.0)] * 12
    one = _bars(seed + [(110.0, 100.0), (99.0, 99.0)])
    two = _bars(seed + [(90.0, 100.0), (100.0, 100.0)])
    res = simulate_universe_basis(
        {"A": one, "B": two}, lookback_bars=12, entry_z=1.5, exit_z=0.5,
        maker_fee_bps=0.0,
    )
    assert res.n_trades == 2
    assert res.n_long == 1
    assert res.n_short == 1
