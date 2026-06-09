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


def test_closes_window_decoupled_from_vwap_window():
    """The trailing close series must NOT be capped at vwap_window.

    Tying them together silently truncated Frame.closes to 60 bars, so a trend
    agent with a lookback > 60 saw an under-length window and never traded. The
    default closes_window is 4×vwap_window, and it is independently overridable.
    """
    candles = [{"t": i * HOUR, "c": 100 + i, "v": 1000} for i in range(300)]
    # default: closes window is wider than the 60-bar vwap window
    last = build_frames({"TST": candles}, vwap_window=60, warmup=60)[-1]
    assert len(last.closes["TST"]) == 240          # 4 × vwap_window, not 60
    assert last.candles_1h["TST"]["n"] == 60       # vwap still uses its own window
    # explicit override is honored
    last2 = build_frames(
        {"TST": candles}, vwap_window=60, closes_window=120, warmup=60
    )[-1]
    assert len(last2.closes["TST"]) == 120


def test_paginate_by_time_walks_past_the_page_cap():
    """A 500-row page cap must not truncate a long window (the funding bug)."""
    from hl_bot.backtest.data import paginate_by_time

    # Simulate an HL endpoint with hourly rows that returns at most 500 per call,
    # oldest-first, honoring startTime. 1200 hourly rows total.
    all_rows = [{"time": i * HOUR, "fundingRate": 0.0001} for i in range(1200)]
    calls: list[int] = []

    def page_fn(start: int, end: int) -> list[dict]:
        calls.append(start)
        window = [r for r in all_rows if start <= r["time"] <= end]
        return window[:500]

    got = paginate_by_time(page_fn, 0, 1200 * HOUR, page_limit=500)
    assert len(got) == 1200, "all rows recovered across pages"
    assert [r["time"] for r in got] == [r["time"] for r in all_rows], "sorted, deduped"
    assert len(calls) >= 3, "needed multiple pages to clear the cap"


def test_paginate_by_time_stops_without_progress():
    """An endpoint that ignores startTime must not loop forever."""
    from hl_bot.backtest.data import paginate_by_time

    fixed = [{"time": 0, "fundingRate": 0.0} for _ in range(500)]
    n_calls = 0

    def page_fn(start: int, end: int) -> list[dict]:
        nonlocal n_calls
        n_calls += 1
        return fixed

    got = paginate_by_time(page_fn, 0, 10 * HOUR, page_limit=500)
    assert len(got) == 1, "deduped by time"
    assert n_calls == 1, "no forward progress -> stop after one page"


def test_fetch_candles_paginates_past_the_row_cap(monkeypatch):
    """fetch_candles must walk the candleSnapshot per-call cap (keyed on ``t``)."""
    from hl_bot.backtest import data

    # Shrink the cap so the test is cheap; simulate an endpoint that returns at
    # most `cap` candles per call, oldest-first, honoring startTime, keyed on `t`.
    cap = 3
    monkeypatch.setattr(data, "CANDLE_PAGE_LIMIT", cap)
    all_rows = [{"t": i * HOUR, "c": 100.0 + i, "v": 1.0} for i in range(10)]
    starts: list[int] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            req = json["req"]
            start, end = req["startTime"], req["endTime"]
            starts.append(start)
            window = [r for r in all_rows if start <= r["t"] <= end]
            return _Resp(window[:cap])

    monkeypatch.setattr(data.httpx, "Client", _FakeClient)

    got = data.fetch_candles("TST", "1h", 0, 10 * HOUR)
    assert [r["t"] for r in got] == [r["t"] for r in all_rows], "all candles, sorted, deduped"
    assert len(starts) >= 3, "needed multiple pages to clear the cap"


def test_build_closes_1h_emits_hourly_series_matching_backtest(monkeypatch):
    """Live `closes` must be real 1h bars (last = latest), not the 60×1m VWAP feed.

    Wiring trend_breakout to paper requires the live view to feed the SAME 1h
    close series the backtest scored on (B1d-trend-deploy). This proves the pure,
    transport-injected loader: per-coin 1h closes, window-capped, page-cap walked,
    and the start time derived from `closes_window` hours back from `now_ms`.
    """
    from hl_bot.backtest import data
    from hl_bot.backtest.data import build_closes_1h

    now_ms = 300 * HOUR  # latest candle is at 299*HOUR; window starts 120h back
    cap = 50  # simulate the candleSnapshot per-call row cap to exercise pagination
    monkeypatch.setattr(data, "CANDLE_PAGE_LIMIT", cap)
    # 300 hourly candles per coin, oldest-first, prices unique per coin/bar.
    rows = {
        "BTC": [{"t": i * HOUR, "c": 100.0 + i, "v": 1.0} for i in range(300)],
        "ETH": [{"t": i * HOUR, "c": 50.0 + i, "v": 1.0} for i in range(300)],
    }
    seen_intervals: set[str] = set()

    def post_fn(payload):
        req = payload["req"]
        seen_intervals.add(req["interval"])
        coin = req["coin"]
        start, end = req["startTime"], req["endTime"]
        window = [r for r in rows.get(coin, []) if start <= r["t"] <= end]
        return window[:cap]

    out = build_closes_1h(post_fn, ["BTC", "ETH"], closes_window=120, now_ms=now_ms)

    assert seen_intervals == {"1h"}, "must request 1-HOUR candles, not 1m"
    # window starts closes_window hours back, so only the last 120 bars are in range
    assert len(out["BTC"]) == 120, "series capped at closes_window"
    assert out["BTC"][-1] == 100.0 + 299, "last element is the latest close"
    assert out["BTC"][0] == 100.0 + 180, "oldest in-window bar (300-120)"
    assert out["ETH"][-1] == 50.0 + 299
    # ascending + deduped despite the page cap forcing multiple fetches
    assert out["BTC"] == sorted(out["BTC"])


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


def test_parse_agent_config_and_factory_override():
    """--config parses to a dict and actually reaches the agent's config."""
    from hl_bot.cli.main import _backtest_factories, parse_agent_config

    # empty / whitespace → defaults
    assert parse_agent_config("") == {}
    assert parse_agent_config("   ") == {}

    # valid JSON object
    cfg = parse_agent_config('{"enter_funding_per_hr": 0.0003, "top_k": 3}')
    assert cfg == {"enter_funding_per_hr": 0.0003, "top_k": 3}

    # non-object / malformed are hard errors, never a silent default
    for bad in ("[1,2]", "5", '"x"', "{not json}"):
        try:
            parse_agent_config(bad)
            raise AssertionError(f"{bad!r} should have raised")
        except ValueError:
            pass

    # the override reaches the constructed agent (not the default)
    conn = init_db(":memory:")
    agent = _backtest_factories(cfg)["xfund_carry_v1"](conn)
    assert agent.cfg.enter_funding_per_hr == 0.0003
    assert agent.cfg.top_k == 3
    # an un-overridden agent keeps its defaults
    default = _backtest_factories({})["xfund_carry_v1"](conn)
    assert default.cfg.enter_funding_per_hr == 0.0001
