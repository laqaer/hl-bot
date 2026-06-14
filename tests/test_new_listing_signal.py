"""build_frames new-listing detection (D2b).

A coin whose candle history begins materially after the dataset's retention-cliff
anchor is flagged as newly listed and carries an age / listing-reference / volume
signal from its FIRST bar — before the vwap warmup, which a day-1 coin can't meet.
"""

from __future__ import annotations

from hl_bot.backtest.data import build_frames

H = 3_600_000  # 1h in ms


def _candles(start_bar: int, n: int, closes: list[float], vol: float = 1000.0):
    return [{"t": (start_bar + i) * H, "c": closes[i], "v": vol} for i in range(n)]


def test_new_listing_flagged_with_ref_and_age():
    # OLD spans bars 0..99 (the retention-cliff anchor); NEW lists at bar 60.
    old = _candles(0, 100, [100.0] * 100)
    new_closes = [10.0, 11.0, 13.0, 14.0, 12.0]  # ref = 10.0, pops to 14
    new = _candles(60, 5, new_closes)

    frames = build_frames({"OLD": old, "NEW": new}, vwap_window=60, warmup=60,
                          bar_hours=1.0, new_listing_gap_bars=12)

    # frames covering the NEW coin's life must flag it; OLD never flagged.
    nl_frames = [f for f in frames if "NEW" in f.new_listings]
    assert len(nl_frames) == 5
    assert all("OLD" not in f.new_listings for f in frames)

    first = nl_frames[0]
    info = first.new_listings["NEW"]
    assert info["age_bars"] == 1            # first bar of its life
    assert info["ref_px"] == 10.0           # first traded close = listing ref
    assert info["vol_usd"] > 0

    last = nl_frames[-1]
    assert last.new_listings["NEW"]["age_bars"] == 5
    assert last.new_listings["NEW"]["ref_px"] == 10.0   # ref stays the listing px


def test_day1_frames_carry_funding_before_warmup():
    # A new coin's day-1 frames (age < warmup) must still carry funding[coin] —
    # the backtester accrues carry only from Frame.funding, and a day-1 position
    # is held entirely within these pre-warmup frames (Codex #20 P2).
    old = _candles(0, 100, [100.0] * 100)
    new = _candles(60, 5, [10.0, 11.0, 13.0, 14.0, 12.0])
    funding = {"NEW": [{"time": 60 * H, "fundingRate": 0.0009}]}  # high fresh-perp funding
    frames = build_frames({"OLD": old, "NEW": new}, funding_by_coin=funding,
                          warmup=30, bar_hours=1.0, new_listing_gap_bars=12)
    nl_frames = [f for f in frames if "NEW" in f.new_listings]
    # every day-1 frame is < warmup but must carry the funding rate, not omit it
    assert nl_frames and all(f.new_listings["NEW"]["age_bars"] < 30 for f in nl_frames)
    assert all(f.funding.get("NEW") == 0.0009 for f in nl_frames)
    assert all(f.funding_hourly.get("NEW") == 0.0009 for f in nl_frames)


def test_no_anchor_gap_means_not_new():
    # Two coins both starting at the cliff: neither is "new" (gap 0 < 12).
    a = _candles(0, 30, [5.0] * 30)
    b = _candles(0, 30, [7.0] * 30)
    frames = build_frames({"A": a, "B": b}, warmup=60, bar_hours=1.0,
                          new_listing_gap_bars=12)
    assert all(not f.new_listings for f in frames)
