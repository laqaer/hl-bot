"""Binance OI-history loader + frame overlay — the offline path to DETERMINE the
S8 OI-crowding edge (host runs the real fetch; CI tests the pure logic).

Pins: the Binance payload parser (dedup/sort/drop-bad), the as-of OI-change
overlay onto candle frames (fractional growth, warmup gaps), and an end-to-end
backtest where overlaid OI drives S8 to trade.
"""

from __future__ import annotations

from hl_bot.agents.oi_crowding_reversal import OICrowdingReversalAgent
from hl_bot.backtest.data import overlay_oi_change
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db
from hl_bot.research.oi_history import hl_to_binance, parse_binance_oi

MIN5 = 300_000


# --- Binance payload parsing -------------------------------------------------

def test_parse_binance_oi_sorts_dedups_drops_bad():
    rows = [
        {"symbol": "BTCUSDT", "sumOpenInterest": "200.0", "timestamp": 2 * MIN5},
        {"symbol": "BTCUSDT", "sumOpenInterest": "100.0", "timestamp": 1 * MIN5},
        {"symbol": "BTCUSDT", "sumOpenInterest": "0", "timestamp": 3 * MIN5},      # zero dropped
        {"symbol": "BTCUSDT", "sumOpenInterest": "bad", "timestamp": 4 * MIN5},    # nan dropped
        {"symbol": "BTCUSDT", "sumOpenInterest": "150.0", "timestamp": 1 * MIN5},  # dup ts -> last wins
    ]
    out = parse_binance_oi(rows)
    assert out == [(1 * MIN5, 150.0), (2 * MIN5, 200.0)]


def test_parse_binance_oi_handles_garbage():
    assert parse_binance_oi(None) == []
    assert parse_binance_oi({"not": "a list"}) == []
    assert parse_binance_oi(["nope", 5, {}]) == []


def test_symbol_mapping_skips_hl_natives():
    assert hl_to_binance("BTC") == "BTCUSDT"
    assert hl_to_binance("kPEPE") == "1000PEPEUSDT"
    assert hl_to_binance("HYPE") is None   # HL-native, no Binance perp


# --- frame overlay -----------------------------------------------------------

def _frames(n, coin="BTC", mid=100.0):
    return [Frame(ts_ms=i * MIN5, mids={coin: mid}) for i in range(n)]


def test_overlay_computes_fractional_change_with_lookback():
    # OI doubles from bar 0 to bar 6 (= 30min lookback at 5m)
    series = [(i * MIN5, 1000.0 + i * 100.0) for i in range(10)]  # +10%/bar
    frames = _frames(10)
    n = overlay_oi_change(frames, {"BTC": series}, lookback_ms=6 * MIN5)
    # bar 6: oi=1600 vs oi@bar0=1000 -> +60%
    assert frames[6].oi_change["BTC"] == 0.6
    # warmup: bars before a full lookback have no signal
    assert "BTC" not in frames[0].oi_change
    assert n > 0


def test_overlay_skips_coins_without_series():
    frames = _frames(8)
    overlay_oi_change(frames, {"ETH": [(i * MIN5, 100.0) for i in range(8)]},
                      lookback_ms=6 * MIN5)
    assert all("BTC" not in f.oi_change for f in frames)   # BTC had no OI series


# --- end-to-end: overlaid OI drives S8 ---------------------------------------

def test_overlaid_oi_drives_s8_backtest():
    coin = "BTC"
    # price: flat, then a +5 sigma overshoot at bar 7, then revert.
    px = [100.0] * 6 + [100.0, 105.0, 100.0, 100.0]
    frames = [Frame(ts_ms=i * MIN5, mids={coin: px[i]},
                    candles_1h={coin: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
                    day_ntl_vlm={coin: 5e7})
              for i in range(len(px))]
    # OI spikes into the overshoot (+50% over the lookback by bar 7)
    oi = [(i * MIN5, 1000.0) for i in range(7)] + [(7 * MIN5, 1500.0),
          (8 * MIN5, 1500.0), (9 * MIN5, 1500.0)]
    overlay_oi_change(frames, {coin: oi}, lookback_ms=6 * MIN5)
    assert frames[7].oi_change[coin] == 0.5

    bt = Backtester(CostModel(maker=False), conn=init_db(":memory:"))
    res = bt.run(OICrowdingReversalAgent(config={"z_enter": 2.0, "oi_spike_min": 0.10},
                                         conn=bt.conn), frames)
    assert res.scorecard.n_trades >= 1
