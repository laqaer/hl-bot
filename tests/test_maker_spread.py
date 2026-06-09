"""Maker-spread capture model — the ninth thesis, an execution edge.

The model rests a passive two-sided maker quote each bar and decomposes every
fill into captured half-spread vs adverse drift. These tests pin the no-lookahead
fill rule and the spread/adverse/fee decomposition on hand-computable bars.
"""

from __future__ import annotations

from hl_bot.backtest.maker_spread import (
    MakerBar,
    bars_from_candles,
    simulate_maker_inventory,
    simulate_maker_spread,
    simulate_universe,
    simulate_universe_inventory,
)


def _flat(mid: float, n: int) -> list[MakerBar]:
    # Bars whose high/low never move off the mid: no quote inside the touch fills.
    return [MakerBar(mid=mid, high=mid, low=mid) for _ in range(n)]


def test_no_movement_no_fills():
    res = simulate_maker_spread(_flat(100.0, 10), half_spread_bps=5.0)
    assert res.n_quotes == 9          # first bar has no prior anchor
    assert res.n_fills == 0
    assert res.net_edge_bps == 0.0
    assert res.net_per_quote_bps == 0.0


def test_first_bar_has_no_quote():
    # A single bar can never quote (no prior mid to anchor on).
    res = simulate_maker_spread([MakerBar(100.0, 110.0, 90.0)], half_spread_bps=5.0)
    assert res.n_quotes == 0
    assert res.n_fills == 0


def test_both_sides_fill_full_roundtrip_earns_spread_minus_fee():
    # Anchor mid 100; hs=10bps -> bid 99.99, ask 100.01. A bar that prints both a
    # high above the ask and a low below the bid fills BOTH sides and closes back
    # at the mid: gross = +10bps/fill, adverse = 0 (mid == anchor), fee default 1.
    bars = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.0, 100.5, 99.5)]
    res = simulate_maker_spread(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_bid_fills == 1
    assert res.n_ask_fills == 1
    assert res.n_both_fills == 1
    assert res.adverse_bps == 0.0
    assert abs(res.gross_spread_bps - 10.0) < 1e-6
    assert abs(res.net_edge_bps - (10.0 - 1.0)) < 1e-6


def test_bid_fill_with_adverse_drift_is_penalised():
    # Anchor 100, hs=10bps -> bid at 99.99. Bar low 99.9 fills the bid; the close
    # falls to 99.95, so we bought at 99.99 and the market kept dropping.
    # gross = (100-99.99)/100 = 10bps; adverse = -(99.95-100)/100 = +5bps.
    bars = [MakerBar(100.0, 100.0, 100.0), MakerBar(99.95, 100.0, 99.9)]
    res = simulate_maker_spread(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_bid_fills == 1
    assert res.n_ask_fills == 0
    assert abs(res.gross_spread_bps - 10.0) < 1e-6
    assert abs(res.adverse_bps - 5.0) < 1e-6
    assert abs(res.net_edge_bps - (10.0 - 5.0 - 1.0)) < 1e-6


def test_ask_fill_with_adverse_drift_is_penalised():
    # Anchor 100, ask at 100.01. Bar high 100.2 fills the ask; close rises to
    # 100.08, so we sold at 100.01 and the market kept rising (adverse for a short).
    bars = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.08, 100.2, 100.0)]
    res = simulate_maker_spread(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_ask_fills == 1
    assert res.n_bid_fills == 0
    assert abs(res.gross_spread_bps - 10.0) < 1e-6
    assert abs(res.adverse_bps - 8.0) < 1e-6
    assert abs(res.net_edge_bps - (10.0 - 8.0 - 1.0)) < 1e-6


def test_rebate_adds_to_net_edge():
    bars = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.0, 100.5, 99.5)]
    base = simulate_maker_spread(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    reb = simulate_maker_spread(
        bars, half_spread_bps=10.0, maker_fee_bps=1.0, maker_rebate_bps=0.5
    )
    assert abs(reb.net_edge_bps - (base.net_edge_bps + 0.5)) < 1e-6


def test_decomposition_identity_holds():
    bars = [
        MakerBar(100.0, 100.0, 100.0),
        MakerBar(99.95, 100.2, 99.9),    # both fill
        MakerBar(100.1, 100.3, 100.0),   # ask fills
    ]
    res = simulate_maker_spread(bars, half_spread_bps=8.0, maker_fee_bps=1.2,
                                maker_rebate_bps=0.3)
    expect = (res.gross_spread_bps - res.adverse_bps - res.fee_bps + res.rebate_bps)
    assert abs(res.net_edge_bps - expect) < 1e-9


def test_universe_pools_fills_equal_to_concatenation():
    a = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.0, 100.5, 99.5)]
    b = [MakerBar(50.0, 50.0, 50.0), MakerBar(49.97, 50.0, 49.94)]
    pooled = simulate_universe({"A": a, "B": b}, half_spread_bps=10.0)
    # 3 fills total: A both-sides (2) + B bid (1).
    assert pooled.n_fills == 3
    assert pooled.n_bid_fills == 2
    assert pooled.n_ask_fills == 1


def test_bars_from_candles_filters_and_orders():
    candles = [
        {"t": 2, "o": "1", "h": "11", "l": "9", "c": "10"},
        {"t": 1, "o": "1", "h": "0", "l": "0", "c": "0"},   # non-positive -> dropped
        {"t": 3, "o": "1", "h": "9", "l": "12", "c": "10"},  # h<l -> dropped
        {"t": 0, "o": "1", "h": "21", "l": "19", "c": "20"},
    ]
    bars = bars_from_candles(candles)
    assert [b.mid for b in bars] == [20.0, 10.0]  # ordered by t, bad rows removed


def test_wider_spread_raises_gross_but_lowers_fill_rate():
    # Mixed bar sizes: small ±10bps bars (only the tight quote fills) interleaved
    # with large ±60bps bars (both quotes fill). Tight quotes fill more often;
    # wide quotes fill rarely but capture more per fill. Pins the core tradeoff.
    bars = [MakerBar(100.0, 100.0, 100.0)]
    for _ in range(10):
        bars.append(MakerBar(100.0, 100.1, 99.9))   # ±10bps — only tight fills
        bars.append(MakerBar(100.0, 100.6, 99.4))   # ±60bps — both fill
    tight = simulate_maker_spread(bars, half_spread_bps=5.0, maker_fee_bps=1.0)
    wide = simulate_maker_spread(bars, half_spread_bps=40.0, maker_fee_bps=1.0)
    assert tight.fill_rate > wide.fill_rate
    assert wide.gross_spread_bps > tight.gross_spread_bps


# --- Inventory-skew / round-trip variant (B-exec slice 2) -------------------


def test_inv_inbar_roundtrip_is_adverse_free():
    # A both-sides-fill bar closes flat: gross = 2*half_spread, adverse = 0,
    # round-trip fee = 2*maker_fee. Net = 2*hs - 2*fee.
    bars = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.0, 100.5, 99.5)]
    res = simulate_maker_inventory(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_round_trips == 1
    assert res.n_inbar_round_trips == 1
    assert res.n_carried_round_trips == 0
    assert res.unclosed_inventory == 0
    assert res.adverse_bps == 0.0
    assert abs(res.gross_spread_bps - 20.0) < 1e-6     # 2 * half_spread
    assert abs(res.fee_bps - 2.0) < 1e-6               # 2 * maker_fee
    assert abs(res.net_edge_bps - (20.0 - 2.0)) < 1e-6


def test_inv_carried_roundtrip_eats_hold_drift():
    # Bar 1: only the bid fills (low 99.9 <= 99.99) -> long lot at 99.99, anchor 100.
    # Bar 2: anchor = bar1 close 99.95; exit ask = 99.95*1.001 = 100.04995, and the
    # bar's high 100.10 fills it. Round-trip: bought 99.99, sold ~100.05.
    #   adverse = -(99.95 - 100)/100 *1e4 = +5.0 bps (mid fell while long)
    #   gross   = hs*(x0+e0)/e0*1e4 = 10*(99.95+100)/100 = 19.995 bps
    #   net     = gross - adverse - 2*fee
    bars = [
        MakerBar(100.0, 100.0, 100.0),
        MakerBar(99.95, 100.0, 99.9),     # bid-only fill -> long lot
        MakerBar(100.06, 100.10, 99.95),  # exit ask fills
    ]
    res = simulate_maker_inventory(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_round_trips == 1
    assert res.n_carried_round_trips == 1
    assert res.n_inbar_round_trips == 0
    assert res.unclosed_inventory == 0
    assert res.avg_hold_bars == 1.0
    assert abs(res.adverse_bps - 5.0) < 1e-6
    assert abs(res.gross_spread_bps - 19.995) < 1e-3
    assert abs(res.net_edge_bps - (res.gross_spread_bps - 5.0 - 2.0)) < 1e-9


def test_inv_decomposition_identity_and_rebate():
    bars = [
        MakerBar(100.0, 100.0, 100.0),
        MakerBar(99.95, 100.0, 99.9),     # long lot
        MakerBar(100.06, 100.10, 99.95),  # exit
        MakerBar(100.0, 100.5, 99.5),     # in-bar round-trip
    ]
    res = simulate_maker_inventory(bars, half_spread_bps=8.0, maker_fee_bps=1.2,
                                   maker_rebate_bps=0.3)
    expect = res.gross_spread_bps - res.adverse_bps - res.fee_bps + res.rebate_bps
    assert abs(res.net_edge_bps - expect) < 1e-9
    assert abs(res.fee_bps - 2.4) < 1e-9      # 2 * 1.2
    assert abs(res.rebate_bps - 0.6) < 1e-9   # 2 * 0.3


def test_inv_holds_at_most_one_lot_no_double_entry():
    # While long, the maker quotes ONLY the exit side: a bar that would also dip to
    # the bid does NOT add a second lot. Bar 2 fills the bid (long). Bar 3's low
    # (99.0) is well below any bid but its high never reaches the exit ask, so we
    # just carry -- no second lot, no round-trip yet.
    bars = [
        MakerBar(100.0, 100.0, 100.0),
        MakerBar(99.95, 100.0, 99.9),    # long lot
        MakerBar(99.0, 99.2, 98.5),      # deep down-bar: would re-fill a bid, but skewed off
    ]
    res = simulate_maker_inventory(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_round_trips == 0
    assert res.unclosed_inventory == 1   # the long lot is still open at series end


def test_inv_unclosed_inventory_is_not_booked():
    # Entry but never an exit -> no realized round-trip, one unclosed lot reported.
    bars = [
        MakerBar(100.0, 100.0, 100.0),
        MakerBar(99.95, 100.0, 99.9),    # long lot, never exits
    ]
    res = simulate_maker_inventory(bars, half_spread_bps=10.0, maker_fee_bps=1.0)
    assert res.n_round_trips == 0
    assert res.net_edge_bps == 0.0
    assert res.unclosed_inventory == 1


def test_inv_universe_pools_round_trips():
    a = [MakerBar(100.0, 100.0, 100.0), MakerBar(100.0, 100.5, 99.5)]   # in-bar RT
    b = [MakerBar(50.0, 50.0, 50.0), MakerBar(50.0, 50.25, 49.75)]      # in-bar RT
    pooled = simulate_universe_inventory({"A": a, "B": b}, half_spread_bps=10.0)
    assert pooled.n_round_trips == 2
    assert pooled.n_inbar_round_trips == 2
