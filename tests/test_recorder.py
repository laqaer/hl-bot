"""Tests for the forward-recording trades→candles archive (backtest/recorder.py)."""

from __future__ import annotations

import pytest

from hl_bot.backtest.data import INTERVAL_MS, build_frames
from hl_bot.backtest.recorder import (
    TradeCandleAggregator,
    append_candles,
    bucket_open_ms,
    load_recorded_candles,
)

MIN = INTERVAL_MS["1m"]


def test_bucket_open_ms_floors_to_interval():
    assert bucket_open_ms(MIN + 12_345, MIN) == MIN
    assert bucket_open_ms(2 * MIN, MIN) == 2 * MIN  # exact boundary opens new bucket


def test_basic_ohlcv_aggregation_across_two_buckets():
    agg = TradeCandleAggregator("1m")
    # bucket 0
    agg.add_trade("BTC", 100.0, 1.0, 1_000)
    agg.add_trade("BTC", 105.0, 2.0, 20_000)
    agg.add_trade("BTC", 98.0, 0.5, 40_000)
    # bucket 1
    agg.add_trade("BTC", 99.0, 1.0, MIN + 1_000)
    candles = agg.pending_candles()
    assert [c["t"] for c in candles] == [0, MIN]
    b0 = candles[0]
    assert b0["o"] == 100.0 and b0["h"] == 105.0 and b0["l"] == 98.0 and b0["c"] == 98.0
    assert b0["v"] == 3.5 and b0["n"] == 3
    assert b0["T"] == MIN - 1 and b0["coin"] == "BTC"
    assert candles[1]["o"] == candles[1]["c"] == 99.0


def test_open_close_track_trade_time_not_arrival_order():
    agg = TradeCandleAggregator("1m")
    # deliver out of order within the same bucket
    agg.add_trade("ETH", 50.0, 1.0, 30_000)
    agg.add_trade("ETH", 48.0, 1.0, 10_000)  # actually earliest -> open
    agg.add_trade("ETH", 52.0, 1.0, 50_000)  # actually latest -> close
    (b,) = agg.pending_candles()
    assert b["o"] == 48.0
    assert b["c"] == 52.0
    assert b["h"] == 52.0 and b["l"] == 48.0


def test_ignores_bad_trades():
    agg = TradeCandleAggregator("1m")
    agg.add_trade("", 100.0, 1.0, 1_000)
    agg.add_trade("BTC", 0.0, 1.0, 1_000)
    agg.add_trade("BTC", -5.0, 1.0, 1_000)
    agg.add_trade("BTC", "nan-ish", 1.0, 1_000)  # type: ignore[arg-type]
    assert agg.pending_candles() == []


def test_flush_completed_leaves_current_bucket():
    agg = TradeCandleAggregator("1m")
    agg.add_trade("BTC", 100.0, 1.0, 10_000)            # bucket 0
    agg.add_trade("BTC", 101.0, 1.0, MIN + 10_000)      # bucket 1
    agg.add_trade("BTC", 102.0, 1.0, 2 * MIN + 10_000)  # bucket 2 (current)
    # "now" lands in bucket 2 -> only buckets 0,1 are completed
    done = agg.flush_completed(2 * MIN + 30_000)
    assert [c["t"] for c in done] == [0, MIN]
    # bucket 2 still held; a second flush at a later time releases it
    assert [c["t"] for c in agg.pending_candles()] == [2 * MIN]
    done2 = agg.flush_completed(3 * MIN + 1_000)
    assert [c["t"] for c in done2] == [2 * MIN]
    assert agg.pending_candles() == []


def test_multi_coin_separation():
    agg = TradeCandleAggregator("1m")
    agg.add_trade("BTC", 100.0, 1.0, 1_000)
    agg.add_trade("ETH", 50.0, 2.0, 1_000)
    done = agg.flush_completed(MIN + 1_000)
    by = {c["coin"]: c for c in done}
    assert by["BTC"]["c"] == 100.0 and by["BTC"]["v"] == 1.0
    assert by["ETH"]["c"] == 50.0 and by["ETH"]["v"] == 2.0


def test_unknown_interval_rejected():
    with pytest.raises(ValueError):
        TradeCandleAggregator("7s")


def test_append_and_load_roundtrip_with_dedup(tmp_path):
    path = tmp_path / "rec.jsonl"
    # first flush
    append_candles(path, [{"coin": "BTC", "t": 0, "T": MIN - 1, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 3, "n": 2}])
    # a later re-flush of the SAME bucket with an updated close — last write wins
    append_candles(path, [{"coin": "BTC", "t": 0, "T": MIN - 1, "o": 1, "h": 9, "l": 1, "c": 8.0, "v": 5, "n": 4}])
    append_candles(path, [{"coin": "ETH", "t": MIN, "T": 2 * MIN - 1, "o": 5, "h": 5, "l": 5, "c": 5, "v": 1, "n": 1}])
    loaded = load_recorded_candles(path)
    assert set(loaded) == {"BTC", "ETH"}
    assert len(loaded["BTC"]) == 1
    assert loaded["BTC"][0]["c"] == 8.0 and loaded["BTC"][0]["n"] == 4  # last line kept


def test_append_empty_and_load_missing(tmp_path):
    path = tmp_path / "none.jsonl"
    assert append_candles(path, []) == 0
    assert load_recorded_candles(path) == {}


def test_recorded_archive_feeds_build_frames(tmp_path):
    """End-to-end: recorded candles are a drop-in for the existing backtester."""
    agg = TradeCandleAggregator("1m")
    px = 100.0
    for i in range(60):
        ts = i * MIN + 1_000
        px += 0.5 if i % 2 == 0 else -0.3
        agg.add_trade("BTC", px, 1.0, ts)
    candles = agg.flush_completed(60 * MIN + 1)  # all 60 buckets completed
    assert len(candles) == 60

    path = tmp_path / "btc.jsonl"
    append_candles(path, candles)
    by_coin = load_recorded_candles(path)

    frames = build_frames(by_coin, vwap_window=10, warmup=10)
    assert frames, "recorded candles should assemble into backtest frames"
    assert all("BTC" in f.mids for f in frames)
    # closes are the recorded candle closes, oldest-first and monotonic in ts
    assert [f.ts_ms for f in frames] == sorted(f.ts_ms for f in frames)
