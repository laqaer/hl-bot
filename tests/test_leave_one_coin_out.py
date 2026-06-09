"""Leave-one-coin-out frame surgery tests.

A dollar-neutral cross-sectional book has no per-coin config knob, so dropping a
coin means removing it from the *data*. ``coins_in_frames`` enumerates the
universe and ``drop_coin`` returns a copy of the frames with one coin removed from
every per-coin field — the cross-sectional analogue of ``leave_one_pair_out``.
These tests pin the pure frame surgery so the durability probe rests on a known
transform (not the network or an agent).
"""

from __future__ import annotations

from hl_bot.backtest.confirm import coins_in_frames, drop_coin
from hl_bot.backtest.engine import Frame


def _frame(ts: int) -> Frame:
    return Frame(
        ts_ms=ts,
        mids={"BTC": 100.0, "ETH": 50.0, "SOL": 10.0},
        funding={"BTC": 0.01, "ETH": 0.02, "SOL": 0.03},
        day_ntl_vlm={"BTC": 9e9, "ETH": 5e9, "SOL": 1e9},
        open_interest={"BTC": 1.0, "ETH": 2.0, "SOL": 3.0},
        candles_1h={"BTC": {"vwap": 100.0}, "ETH": {"vwap": 50.0}, "SOL": {"vwap": 10.0}},
        closes={"BTC": [99.0, 100.0], "ETH": [49.0, 50.0], "SOL": [9.0, 10.0]},
        spot_mids={"BTC": 100.1, "ETH": 50.1, "SOL": 10.1},
        liquidations=[{"coin": "BTC", "sz": 1.0}],
    )


def test_coins_in_frames_is_deduped_order_preserving():
    frames = [_frame(1), _frame(2)]
    assert coins_in_frames(frames) == ["BTC", "ETH", "SOL"]


def test_coins_in_frames_unions_across_frames():
    f1 = _frame(1)
    f2 = _frame(2)
    f2.mids = {"ETH": 50.0, "DOGE": 0.1}  # a coin only present in the 2nd frame
    assert coins_in_frames([f1, f2]) == ["BTC", "ETH", "SOL", "DOGE"]


def test_drop_coin_removes_from_every_per_coin_field():
    frames = [_frame(1), _frame(2)]
    out = drop_coin(frames, "ETH")
    for f in out:
        for field in (f.mids, f.funding, f.day_ntl_vlm, f.open_interest,
                      f.candles_1h, f.closes, f.spot_mids):
            assert "ETH" not in field
        # the other coins survive untouched
        assert set(f.mids) == {"BTC", "SOL"}
        assert f.closes["BTC"] == [99.0, 100.0]


def test_drop_coin_is_pure_inputs_untouched():
    frames = [_frame(1)]
    _ = drop_coin(frames, "ETH")
    assert "ETH" in frames[0].mids  # original frame is not mutated


def test_drop_coin_preserves_ts_and_liquidations():
    frames = [_frame(7)]
    out = drop_coin(frames, "SOL")
    assert out[0].ts_ms == 7
    # liquidations are an event list, not keyed by coin -> carried through as-is
    assert out[0].liquidations == [{"coin": "BTC", "sz": 1.0}]


def test_drop_absent_coin_is_a_noop_copy():
    frames = [_frame(1)]
    out = drop_coin(frames, "NOPE")
    assert coins_in_frames(out) == ["BTC", "ETH", "SOL"]
    assert out[0] is not frames[0]  # still a fresh copy
