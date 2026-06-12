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


def test_build_frames_carries_intrabar_highs_lows():
    """B-FILL2: candle h/l land on the frame; bad/missing rows degrade silently."""
    candles = [
        {"t": i * HOUR, "c": 100.0, "v": 1000, "h": 101.0 + i, "l": 99.0 - i}
        for i in range(64)
    ]
    candles[63] = {"t": 63 * HOUR, "c": 100.0, "v": 1000}                      # no h/l
    candles[62] = {"t": 62 * HOUR, "c": 100.0, "v": 1000, "h": 90.0, "l": 95}  # l > h garbage
    frames = build_frames({"TST": candles}, vwap_window=60, warmup=60)
    by_ts = {f.ts_ms: f for f in frames}

    ok = by_ts[61 * HOUR]
    assert ok.highs["TST"] == 101.0 + 61 and ok.lows["TST"] == 99.0 - 61
    assert "TST" not in by_ts[62 * HOUR].highs and "TST" not in by_ts[62 * HOUR].lows
    assert "TST" not in by_ts[63 * HOUR].highs and "TST" not in by_ts[63 * HOUR].lows
    # mids are unaffected by missing extremes
    assert by_ts[63 * HOUR].mids["TST"] == 100.0


def test_legacy_cache_without_highs_lows_still_loads(tmp_path):
    """Pre-B-FILL2 cached frames (no highs/lows keys) load with empty extremes."""
    import gzip
    import json

    from hl_bot.backtest.data import load_cached_frames, save_frames

    legacy = {"ts_ms": 0, "mids": {"TST": 100.0}, "funding": {}, "funding_hourly": {},
              "day_ntl_vlm": {}, "candles_1h": {}, "closes": {}, "spot_mids": {},
              "liquidations": []}
    p = tmp_path / "legacy.json.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump([legacy], fh)
    loaded = load_cached_frames(p)
    assert loaded[0].highs == {} and loaded[0].lows == {}

    # and fresh frames round-trip the extremes
    f = Frame(ts_ms=0, mids={"TST": 100.0}, highs={"TST": 101.0}, lows={"TST": 99.0})
    p2 = save_frames(tmp_path / "fresh.json.gz", [f])
    got = load_cached_frames(p2)[0]
    assert got.highs == {"TST": 101.0} and got.lows == {"TST": 99.0}


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


def test_default_cache_path_keys_on_vwap_window():
    """Frames bake the VWAP window in, so a non-default window needs its own file.

    The default window keeps the historical key so caches fetched before the
    --vwap-window option existed stay valid.
    """
    from hl_bot.backtest.data import default_cache_path

    legacy = default_cache_path(["BTC", "ETH"], "1h", 90)
    assert legacy.name == "BTC-ETH_1h_90d.json.gz"
    assert default_cache_path(["BTC", "ETH"], "1h", 90, vwap_window=60) == legacy

    windowed = default_cache_path(["BTC", "ETH"], "15m", 52, vwap_window=4)
    assert windowed.name == "BTC-ETH_15m_52d_w4.json.gz"
    assert windowed != default_cache_path(["BTC", "ETH"], "15m", 52)


def test_cached_or_fetch_window_never_serves_wrong_window(tmp_path, monkeypatch):
    """A window-60 cache must not satisfy a window-4 request (and vice versa)."""
    import hl_bot.config as config
    from hl_bot.backtest import data

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    frames_w60 = _mean_reversion_path()
    data.save_frames(data.default_cache_path(["TST"], "15m", 5), frames_w60)

    fetched: list[int] = []

    def fake_load_frames(coins, *, vwap_window=60, **kw):
        fetched.append(vwap_window)
        return frames_w60

    monkeypatch.setattr(data, "load_frames", fake_load_frames)

    # default window: served from the existing cache, no fetch
    got = data.cached_or_fetch(["TST"], interval="15m", days=5)
    assert len(got) == len(frames_w60) and fetched == []

    # non-default window: cache miss → fetch with that window, new file written
    data.cached_or_fetch(["TST"], interval="15m", days=5, vwap_window=4)
    assert fetched == [4]
    assert data.default_cache_path(["TST"], "15m", 5, vwap_window=4).exists()

    # second call hits the new window-keyed cache, no second fetch
    data.cached_or_fetch(["TST"], interval="15m", days=5, vwap_window=4)
    assert fetched == [4]


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


# ---------------------------------------------------------------------------
# build_frames linear-time rewrite: must match the original per-frame scans
# ---------------------------------------------------------------------------


def _build_frames_naive(candles_by_coin, funding_by_coin=None, *,
                        vwap_window=60, warmup=60, bar_hours=1.0):
    """The original O(n²) build_frames logic, kept verbatim as the reference."""
    from hl_bot.backtest.data import closes_vols, funding_rate_at

    funding_by_coin = funding_by_coin or {}
    by_ts, all_ts, series = {}, set(), {}
    for coin, candles in candles_by_coin.items():
        ordered = sorted(candles, key=lambda k: int(k.get("t", 0)))
        by_ts[coin] = {int(k.get("t", 0)): k for k in ordered}
        series[coin] = closes_vols(ordered)
        all_ts.update(by_ts[coin].keys())
    frames = []
    for ts in sorted(all_ts):
        mids, vol, candles_1h, closes_window, funding = {}, {}, {}, {}, {}
        for coin, idx in by_ts.items():
            k = idx.get(ts)
            if not k:
                continue
            try:
                mid = float(k.get("c", 0))
            except (TypeError, ValueError):
                continue
            if mid <= 0:
                continue
            mids[coin] = mid
            closes, vols, tss = series[coin]
            upto = [i for i, t in enumerate(tss) if t <= ts]
            if len(upto) < warmup:
                continue
            cut = upto[-1] + 1
            vwap, sigma = rolling_vwap_sigma(closes[:cut], vols[:cut], vwap_window)
            if vwap is not None and sigma is not None:
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": min(cut, vwap_window)}
            closes_window[coin] = closes[max(0, cut - vwap_window):cut]
            vol[coin] = sum(vols[max(0, cut - 1440):cut]) * mid
            funding[coin] = funding_rate_at(funding_by_coin.get(coin, []), ts) * bar_hours
        if mids:
            frames.append(Frame(ts_ms=ts, mids=mids, funding=funding, day_ntl_vlm=vol,
                                candles_1h=candles_1h, closes=closes_window))
    return frames


def _irregular_dataset():
    """3 coins with missing bars, a zero-close candle, and real-shaped funding."""
    import random

    rng = random.Random(42)
    candles_by_coin, funding_by_coin = {}, {}
    for ci, coin in enumerate(("AAA", "BBB", "CCC")):
        px = 100.0 * (ci + 1)
        candles = []
        for i in range(150):
            if rng.random() < 0.1 * ci:        # BBB/CCC have gaps
                continue
            px *= 1 + rng.uniform(-0.01, 0.01)
            candles.append({"t": i * HOUR, "c": px, "v": rng.uniform(100, 2000)})
        candles.append({"t": 40 * HOUR + 1, "c": 0, "v": 50})   # invalid close
        candles_by_coin[coin] = candles
        # hourly funding, ascending (the API shape), with one malformed row
        funding_by_coin[coin] = [
            {"time": i * HOUR, "fundingRate": rng.uniform(-3e-4, 3e-4)}
            for i in range(150)
        ]
        funding_by_coin[coin].insert(5, {"time": "bogus", "fundingRate": None})
    return candles_by_coin, funding_by_coin


def test_build_frames_matches_naive_reference():
    """The cursor/prefix-sum rewrite reproduces the original scan exactly."""
    import pytest

    candles_by_coin, funding_by_coin = _irregular_dataset()
    kw = {"vwap_window": 24, "warmup": 30, "bar_hours": 0.5}
    got = build_frames(candles_by_coin, funding_by_coin=funding_by_coin, **kw)
    want = _build_frames_naive(candles_by_coin, funding_by_coin, **kw)

    assert len(got) == len(want) and got, "same number of frames"
    for g, w in zip(got, want, strict=True):
        assert g.ts_ms == w.ts_ms
        assert g.mids == w.mids
        assert g.closes == w.closes
        assert set(g.candles_1h) == set(w.candles_1h)
        for coin, stats in w.candles_1h.items():
            assert g.candles_1h[coin]["n"] == stats["n"]
            assert g.candles_1h[coin]["vwap"] == pytest.approx(stats["vwap"], rel=1e-12)
            assert g.candles_1h[coin]["sigma"] == pytest.approx(stats["sigma"], rel=1e-12)
        for coin, v in w.day_ntl_vlm.items():
            assert g.day_ntl_vlm[coin] == pytest.approx(v, rel=1e-9)
        for coin, f in w.funding.items():
            assert g.funding[coin] == pytest.approx(f, rel=1e-12, abs=1e-18)


def test_build_frames_sorts_unsorted_funding():
    """Funding rows arriving out of order still yield 'most recent rate ≤ ts'."""
    candles = [{"t": i * HOUR, "c": 100.0, "v": 10.0} for i in range(8)]
    shuffled = [
        {"time": 4 * HOUR, "fundingRate": 4e-4},
        {"time": 1 * HOUR, "fundingRate": 1e-4},
        {"time": 3 * HOUR, "fundingRate": 3e-4},
    ]
    frames = build_frames({"TST": candles}, funding_by_coin={"TST": shuffled},
                          vwap_window=4, warmup=2)
    by_ts = {f.ts_ms: f.funding.get("TST") for f in frames}
    assert by_ts[2 * HOUR] == 1e-4      # only the t=1h row is in effect
    assert by_ts[3 * HOUR] == 3e-4
    assert by_ts[7 * HOUR] == 4e-4      # latest-by-time, not latest-in-list


def test_build_frames_funding_hourly_is_unscaled():
    """frame.funding is per-bar (accrual); frame.funding_hourly is the raw rate.

    Agents read view.funding as an HOURLY rate (live activeAssetCtx semantics),
    so the builder must carry both series or a rate-threshold lever would see
    values 60× too small on 1m bars.
    """
    candles = [{"t": i * HOUR // 60, "c": 100.0, "v": 10.0} for i in range(8)]
    funding = [{"time": 0, "fundingRate": 6e-4}]
    frames = build_frames({"TST": candles}, funding_by_coin={"TST": funding},
                          vwap_window=4, warmup=2, bar_hours=1 / 60)
    last = frames[-1]
    assert last.funding_hourly["TST"] == 6e-4
    assert abs(last.funding["TST"] - 1e-5) < 1e-15      # 6e-4 / 60


def test_build_frames_coarse_bars_sum_hourly_settlements():
    """Bars >1h integrate the actual hourly funding rows inside the bar.

    Extrapolating the last sampled rate × bar_hours pays an extreme print for
    a whole 4h/1d bar while real funding mean-reverts within hours — which
    flatters exactly the carry strategies coarse backtests exist to test. The
    pre-bar rate must not leak into the sum, and funding_hourly stays the raw
    last-seen rate.
    """
    four_h = 4 * HOUR
    candles = [{"t": i * four_h, "c": 100.0, "v": 10.0} for i in range(4)]
    funding = [{"time": 0, "fundingRate": 9e-4}] + [
        # second bar (4h, 8h]: settlements at 5..8h sum to 1+2+3+4 = 10e-4
        {"time": (5 + j) * HOUR, "fundingRate": (j + 1) * 1e-4}
        for j in range(4)
    ]
    frames = build_frames({"TST": candles}, funding_by_coin={"TST": funding},
                          vwap_window=2, warmup=2, bar_hours=4.0)
    by_ts = {f.ts_ms: f for f in frames}
    bar2 = by_ts[2 * four_h]
    assert abs(bar2.funding["TST"] - 10e-4) < 1e-15     # in-bar sum, no 9e-4 leak
    assert bar2.funding_hourly["TST"] == 4e-4           # raw last rate, unscaled
    # a coarse bar with no settlements accrues nothing (vs stale-rate × 4)
    bar3 = by_ts[3 * four_h]
    assert bar3.funding["TST"] == 0.0
    assert bar3.funding_hourly["TST"] == 4e-4


class _FundingProbe(TwapMrAgent):
    """Records the funding rates the engine shows the agent each tick."""

    def __init__(self, conn):
        super().__init__(config={}, conn=conn)
        self.seen: list[float] = []

    def decide(self, view):
        if "TST" in view.funding:
            self.seen.append(view.funding["TST"])
        return super().decide(view)


def test_engine_view_funding_is_hourly_with_per_bar_fallback():
    conn = init_db(":memory:")
    f = _frame(0, 100.0)
    f.funding = {"TST": 1e-5}                 # per-bar (1m share)
    f.funding_hourly = {"TST": 6e-4}          # raw hourly
    legacy = _frame(1, 100.0)
    legacy.funding = {"TST": 2e-4}            # pre-funding_hourly cache shape
    probe = _FundingProbe(conn)
    Backtester(CostModel(maker=True), conn=conn).run(probe, [f, legacy])
    assert probe.seen == [6e-4, 2e-4]


def test_ensure_funding_hourly_backfills_legacy_caches():
    from dataclasses import replace

    from hl_bot.backtest.data import ensure_funding_hourly

    legacy = _frame(0, 100.0)
    legacy.funding = {"TST": 1e-5}                       # hourly 6e-4 at 1m bars
    fresh = replace(legacy, funding_hourly={"TST": 6e-4})
    out = ensure_funding_hourly([legacy, fresh], bar_hours=1 / 60)
    assert abs(out[0].funding_hourly["TST"] - 6e-4) < 1e-15
    assert out[1].funding_hourly == {"TST": 6e-4}        # already-correct frames untouched


def test_cached_or_fetch_backfills_funding_hourly(tmp_path, monkeypatch):
    """A cache written before funding_hourly existed still serves hourly rates."""
    import hl_bot.config as config
    from hl_bot.backtest import data

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    legacy = _frame(0, 100.0)
    legacy.funding = {"TST": 1e-5}
    legacy.funding_hourly = {}
    data.save_frames(data.default_cache_path(["TST"], "1m", 3), [legacy])

    got = data.cached_or_fetch(["TST"], interval="1m", days=3)
    assert abs(got[0].funding_hourly["TST"] - 6e-4) < 1e-15


def test_build_frames_scales_to_fine_intervals():
    """20k 1m bars × 2 coins must build fast (was quadratic: minutes, not <2s)."""
    minute = 60_000
    candles_by_coin = {
        coin: [{"t": i * minute, "c": 100.0 + (i % 7), "v": 5.0} for i in range(20_000)]
        for coin in ("AAA", "BBB")
    }
    frames = build_frames(candles_by_coin, vwap_window=60, warmup=60, bar_hours=1 / 60)
    assert len(frames) == 20_000
    last = frames[-1]
    assert set(last.mids) == {"AAA", "BBB"}
    assert last.candles_1h["AAA"]["sigma"] > 0
    assert last.day_ntl_vlm["AAA"] > 0
