"""Backtest engine tests — synthetic frames, no network.

These pin down the three things the engine must get right to be trustworthy:
  1. A genuine mean-reversion path is scored as profitable (signs/PnL correct).
  2. Execution cost matters: the same path nets strictly less as a taker.
  3. The simulated clock drives hold-based exits (time.time() injection works).
"""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.data import build_frames, rolling_vwap_sigma
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000


def _frame(ts_h: int, mid: float, *, vwap: float = 100.0, sigma: float = 1.0,
           coin: str = "TST") -> Frame:
    return Frame(
        ts_ms=ts_h * HOUR,
        mids={coin: mid},
        funding={coin: 0.0},
        day_ntl_vlm={coin: 50_000_000.0},
        candles_1h={coin: {"vwap": vwap, "sigma": sigma, "n": 60}},
    )


def _mean_reversion_path() -> list[Frame]:
    # flat at vwap, spike up (short), revert (cover), dip (long), revert (cover)
    return [
        _frame(0, 100.0),
        _frame(1, 103.0),   # z=+3 -> short
        _frame(2, 100.0),   # revert -> exit short, profit
        _frame(3, 97.0),    # z=-3 -> long
        _frame(4, 100.0),   # revert -> exit long, profit
    ]


def test_mean_reversion_is_profitable_without_costs():
    conn = init_db(":memory:")
    agent = TwapMrAgent(config={}, conn=conn)
    bt = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn)
    res = bt.run(agent, _mean_reversion_path())

    assert res.scorecard.n_trades >= 4          # 2 opens + 2 closes
    assert res.net_pnl > 5.0                     # ~ +12 on the path
    assert res.edge_bps is not None and res.edge_bps > 0
    # fills actually landed in the DB and score from the same path
    n_fills = conn.execute("SELECT COUNT(*) FROM fills WHERE agent='twap_mr_v1'").fetchone()[0]
    assert n_fills == res.scorecard.n_trades


def test_taker_costs_reduce_pnl():
    path = _mean_reversion_path()

    conn_m = init_db(":memory:")
    maker = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn_m)
    res_maker = maker.run(TwapMrAgent(config={}, conn=conn_m), path)

    conn_t = init_db(":memory:")
    taker = Backtester(
        CostModel(maker=False, taker_fee_bps=4.5, slippage_bps=2.0), conn=conn_t)
    res_taker = taker.run(TwapMrAgent(config={}, conn=conn_t), path)

    assert res_taker.net_pnl < res_maker.net_pnl
    assert res_taker.scorecard.fees_paid > 0


def test_simulated_clock_drives_max_hold_exit():
    # spike then sit at z=+1 (no revert, no stop) until 4h max-hold fires.
    frames = [
        _frame(0, 100.0),
        _frame(1, 103.0),   # short entry
        _frame(2, 101.0),   # z=1 -> hold
        _frame(3, 101.0),   # hold
        _frame(4, 101.0),   # hold
        _frame(5, 101.0),   # hold_hrs >= 4 -> MAX-HOLD exit
        _frame(6, 101.0),
    ]
    conn = init_db(":memory:")
    agent = TwapMrAgent(config={}, conn=conn)
    bt = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn)
    bt.run(agent, frames)

    flats = conn.execute(
        "SELECT reasoning FROM agent_decisions WHERE action='flatten'"
    ).fetchall()
    assert any("MAX-HOLD" in (r[0] or "") for r in flats)


def test_rolling_vwap_sigma_pure():
    closes = [10.0, 12.0, 8.0, 10.0]
    vols = [1.0, 1.0, 1.0, 1.0]
    vwap, sigma = rolling_vwap_sigma(closes, vols, window=4)
    assert vwap == 10.0
    assert sigma and sigma > 0


def test_build_frames_from_candles():
    # 80 hourly candles oscillating around 100
    candles = [
        {"t": i * HOUR, "c": 100 + (2 if i % 2 else -2), "v": 1000}
        for i in range(80)
    ]
    frames = build_frames({"TST": candles}, vwap_window=60, warmup=60)
    assert frames, "expected frames after warmup"
    last = frames[-1]
    assert "TST" in last.mids
    assert "TST" in last.candles_1h
    assert last.candles_1h["TST"]["sigma"] > 0


def test_paginate_funding_covers_full_window():
    from hl_bot.backtest.data import _paginate_funding

    # synthetic hourly funding rows spanning ~42 days (1008h) — well past one
    # 500-row page; the fetcher must page through to cover the whole window.
    HR = 3_600_000
    total = 1008
    all_rows = [{"time": i * HR, "fundingRate": 0.0001} for i in range(total)]
    calls: list[tuple[int, int]] = []

    def fake_page(start: int, end: int):
        calls.append((start, end))
        page = [r for r in all_rows if start <= r["time"] <= end]
        return page[:500]  # HL's hard cap

    rows = _paginate_funding(fake_page, 0, (total - 1) * HR)
    times = [int(r["time"]) for r in rows]
    assert len(rows) == total, "should reassemble every row across pages"
    assert times == sorted(set(times)), "rows unique and time-ordered"
    assert len(calls) >= 3, "a >1000-row window needs multiple 500-row pages"


def test_paginate_funding_stops_on_short_page():
    from hl_bot.backtest.data import _paginate_funding

    HR = 3_600_000
    rows_src = [{"time": i * HR, "fundingRate": -0.0002} for i in range(120)]
    n_calls = 0

    def fake_page(start: int, end: int):
        nonlocal n_calls
        n_calls += 1
        return [r for r in rows_src if start <= r["time"] <= end][:500]

    rows = _paginate_funding(fake_page, 0, 119 * HR)
    assert len(rows) == 120
    assert n_calls == 1, "a <500-row first page is the last page — no extra calls"


def test_paginate_candles_covers_full_window():
    from hl_bot.backtest.data import _paginate_candles

    # synthetic 1m candles spanning ~10 days (14400 bars) — well past one
    # ~5000-row page. The fake mirrors HL's real semantics: the page is anchored
    # to ``end`` (returns the *newest* ≤5000 rows within [start, end]), so the
    # paginator must walk *backward* to reassemble the whole window.
    MIN = 60_000
    total = 14_400
    all_rows = [{"t": i * MIN, "c": 100.0 + i} for i in range(total)]
    calls: list[tuple[int, int]] = []

    def fake_page(start: int, end: int):
        calls.append((start, end))
        page = [r for r in all_rows if start <= r["t"] <= end]
        return page[-5000:]  # HL's hard cap, anchored to endTime

    rows = _paginate_candles(fake_page, 0, (total - 1) * MIN, MIN)
    ts = [int(r["t"]) for r in rows]
    assert len(rows) == total, "should reassemble every candle across pages"
    assert ts == sorted(set(ts)), "candles unique and time-ordered"
    assert len(calls) >= 3, "a >10000-row window needs multiple ~5000-row pages"


def test_paginate_candles_stops_on_short_page():
    from hl_bot.backtest.data import _paginate_candles

    MIN = 60_000
    rows_src = [{"t": i * MIN, "c": 50.0} for i in range(200)]
    n_calls = 0

    def fake_page(start: int, end: int):
        nonlocal n_calls
        n_calls += 1
        return [r for r in rows_src if start <= r["t"] <= end][-5000:]

    rows = _paginate_candles(fake_page, 0, 199 * MIN, MIN)
    assert len(rows) == 200
    assert n_calls == 1, "a sub-cap first page covers the window — no extra calls"


def test_paginate_candles_stops_when_no_older_history():
    from hl_bot.backtest.data import _paginate_candles

    # HL retains only ~one cap of history: nothing exists older than the trailing
    # block, so a request whose floor predates available data must terminate after
    # the data runs out rather than looping to max_pages.
    MIN = 60_000
    retained = [{"t": i * MIN, "c": 1.0} for i in range(5200, 5200 + 300)]  # only newest 300 exist
    n_calls = 0

    def fake_page(start: int, end: int):
        nonlocal n_calls
        n_calls += 1
        return [r for r in retained if start <= r["t"] <= end][-5000:]

    rows = _paginate_candles(fake_page, 0, 5499 * MIN, MIN)
    assert len(rows) == 300, "returns all retained candles"
    assert n_calls <= 3, "stops once the page yields no new (older) rows, not at max_pages"


def test_frame_cache_roundtrip(tmp_path):
    from hl_bot.backtest.data import load_cached_frames, save_frames
    frames = _mean_reversion_path()
    p = save_frames(tmp_path / "c" / "frames.json.gz", frames)
    assert p.exists()
    loaded = load_cached_frames(p)
    assert len(loaded) == len(frames)
    assert loaded[1].mids == frames[1].mids
    assert loaded[1].candles_1h == frames[1].candles_1h
    # a cached run scores identically to a fresh run
    from hl_bot.backtest.engine import Backtester, CostModel
    from hl_bot.db.schema import init_db
    c1 = init_db(":memory:")
    c2 = init_db(":memory:")
    r1 = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=c1).run(TwapMrAgent(config={}, conn=c1), frames)
    r2 = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=c2).run(TwapMrAgent(config={}, conn=c2), loaded)
    assert r1.net_pnl == r2.net_pnl


def test_window_bounds_trailing_and_historical():
    from hl_bot.backtest.data import window_bounds

    day_ms = 86_400_000
    # explicit end_ms → exact, deterministic [start, end] of the right length
    start, end = window_bounds(120, end_ms=1_000 * day_ms)
    assert end == 1_000 * day_ms
    assert start == (1_000 - 120) * day_ms
    # a disjoint older window (end shifted back 120d) abuts but never overlaps
    o_start, o_end = window_bounds(120, end_ms=(1_000 - 120) * day_ms)
    assert o_end == start              # older window ends where the trailing one starts
    assert o_start < o_end <= start    # strictly in the past, no overlap
    # default (now) trails the clock
    t_start, t_end = window_bounds(30)
    assert t_end - t_start == 30 * day_ms


def test_default_cache_path_window_keying():
    from hl_bot.backtest.data import default_cache_path

    coins = ["SOL", "ETH", "BTC"]
    trailing = default_cache_path(coins, "1h", 120)
    # trailing window keeps the legacy key (existing caches still resolve)
    assert trailing.name == "BTC-ETH-SOL_1h_120d.json.gz"
    # a historical window lands in a distinct, end-date-tagged file
    hist = default_cache_path(coins, "1h", 120, end_ms=1_700_000_000_000)
    assert hist != trailing
    assert "_end" in hist.name
    # same end_ms is stable; a different end_ms is a different file
    assert hist == default_cache_path(coins, "1h", 120, end_ms=1_700_000_000_000)
    assert hist != default_cache_path(coins, "1h", 120, end_ms=1_690_000_000_000)


def _resp(status: int, headers: dict | None = None):
    import httpx

    return httpx.Response(
        status, headers=headers or {}, request=httpx.Request("POST", "http://x/info")
    )


def test_request_with_retry_returns_first_success():
    from hl_bot.backtest.data import _request_with_retry

    calls = 0

    def do_request():
        nonlocal calls
        calls += 1
        return _resp(200)

    slept: list[float] = []
    r = _request_with_retry(do_request, sleep=slept.append)
    assert r.status_code == 200
    assert calls == 1, "a 200 on the first try makes no extra calls"
    assert slept == [], "no backoff on immediate success"


def test_request_with_retry_recovers_from_429():
    from hl_bot.backtest.data import _request_with_retry

    # 429 then 503, then a 200 — the fetch must complete, not abort the window.
    # seq.pop() takes the last element, so list is reversed: returns 429, 503, 200.
    seq = [_resp(200), _resp(503), _resp(429)]
    slept: list[float] = []
    r = _request_with_retry(seq.pop, base_delay=1.0, sleep=slept.append)
    assert r.status_code == 200
    assert len(slept) == 2, "two transient failures → two backoffs"
    assert slept[0] < slept[1], "exponential backoff grows between attempts"


def test_request_with_retry_honors_retry_after_header():
    from hl_bot.backtest.data import _request_with_retry

    seq = [_resp(200), _resp(429, {"Retry-After": "7"})]
    slept: list[float] = []
    _request_with_retry(seq.pop, base_delay=1.0, sleep=slept.append)
    assert slept == [7.0], "Retry-After header overrides exponential backoff"


def test_request_with_retry_raises_after_exhausting():
    import httpx
    import pytest

    from hl_bot.backtest.data import _request_with_retry

    slept: list[float] = []
    with pytest.raises(httpx.HTTPStatusError):
        _request_with_retry(
            lambda: _resp(429), max_retries=3, base_delay=1.0, sleep=slept.append
        )
    assert len(slept) == 3, "retried exactly max_retries times before surfacing"
