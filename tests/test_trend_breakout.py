"""Evidence tests for trend_breakout_v1.

The trend-follow thesis is the mirror of TWAP-MR: ride a breakout instead of
fading it. So the tests that matter are directional — on a sustained uptrend the
agent goes LONG and profits (where the fader bleeds), on a sustained downtrend it
goes SHORT, and a Donchian reversal flips it back out.
"""

from __future__ import annotations

from hl_bot.agents.trend_breakout import TrendBreakoutAgent, efficiency_ratio
from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000
COIN = "TST"
CFG = {"entry_lookback": 6, "exit_lookback": 3, "stop_loss_pct": 0.5}


def _frames_from_closes(closes_series: list[float]) -> list[Frame]:
    """Build frames where each bar carries the trailing closes up to that bar."""
    frames = []
    trailing: list[float] = []
    for i, mid in enumerate(closes_series):
        trailing.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={COIN: mid},
            funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            closes={COIN: list(trailing)},
        ))
    return frames


def _run(agent_cls, frames, config=None):
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn)  # cost-free: isolate direction
    return bt.run(agent_cls(config=config or {}, conn=conn), frames), conn


def _sides(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT side FROM agent_decisions WHERE action='place' ORDER BY ts_ms").fetchall()]


def test_goes_long_and_profits_on_uptrend():
    # Flat base then a clean breakout that keeps ripping.
    closes = [100.0] * 6 + [100.0 + 2.0 * i for i in range(1, 14)]
    frames = _frames_from_closes(closes)
    res, conn = _run(TrendBreakoutAgent, frames, CFG)
    assert res.net_pnl > 0
    assert "B" in _sides(conn)  # entered LONG with the trend
    # And it does what the fader can't: TWAP fading the same rip bleeds.
    twap_cfg = {"sigma_enter": 2.0, "sigma_exit": 0.5}
    # give TWAP the vwap/sigma it needs by reusing the same uptrend
    tw_frames = []
    trailing: list[float] = []
    for i, mid in enumerate(closes):
        trailing.append(mid)
        tw_frames.append(Frame(
            ts_ms=i * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": mid - 6.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(trailing)},
        ))
    tw_res, _ = _run(TwapMrAgent, tw_frames, twap_cfg)
    assert res.net_pnl > tw_res.net_pnl


def test_goes_short_on_downtrend():
    closes = [100.0] * 6 + [100.0 - 2.0 * i for i in range(1, 14)]
    frames = _frames_from_closes(closes)
    res, conn = _run(TrendBreakoutAgent, frames, CFG)
    assert res.net_pnl > 0
    assert "A" in _sides(conn)  # entered SHORT with the down-trend


def test_no_breakout_no_trade_when_range_bound():
    # Oscillation inside a fixed band: no new N-bar high/low -> no entries.
    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0] * 4
    frames = _frames_from_closes(closes)
    res, conn = _run(TrendBreakoutAgent, frames, CFG)
    assert _sides(conn) == []
    assert res.scorecard.n_trades == 0


def test_efficiency_ratio_separates_trend_from_chop():
    # Straight line up = ER 1.0 (all net move, no wasted path); a zig-zag that
    # ends where it started = ER 0.0 (all path, no net move).
    assert efficiency_ratio([1.0, 2.0, 3.0, 4.0]) == 1.0
    assert efficiency_ratio([1.0, 2.0, 1.0, 2.0, 1.0]) == 0.0
    assert efficiency_ratio([1.0]) is None


def test_regime_gate_skips_breakout_in_chop():
    # A breakout to a new 6-bar high, but the broader context wandered there
    # (whipsaw chop): with the regime gate ON the entry is vetoed; OFF it fires.
    closes = [100.0, 103.0, 99.0, 104.0, 100.0, 105.0, 101.0, 106.0]
    frames = _frames_from_closes(closes)
    er = efficiency_ratio(closes)
    assert er is not None and er < 0.5  # genuinely choppy path

    gated = {**CFG, "min_efficiency_ratio": 0.5, "regime_lookback": 60}
    res_on, conn_on = _run(TrendBreakoutAgent, frames, gated)
    assert _sides(conn_on) == []  # chop regime → sat out

    res_off, conn_off = _run(TrendBreakoutAgent, frames, CFG)
    assert "B" in _sides(conn_off)  # same breakout taken when gate is off


def test_trailing_channel_exits_on_reversal():
    # Rip up (enter long), then sharp reversal below the exit channel -> flatten.
    up = [100.0] * 6 + [100.0 + 2.0 * i for i in range(1, 8)]   # breakout long
    down = [up[-1] - 4.0 * i for i in range(1, 6)]              # reversal
    frames = _frames_from_closes(up + down)
    _, conn = _run(TrendBreakoutAgent, frames, CFG)
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM agent_decisions WHERE action IN ('place','flatten') ORDER BY ts_ms").fetchall()]
    assert "place" in actions and "flatten" in actions


def _decisions(view) -> set[tuple[str, str | None, str | None]]:
    """(action, coin, side) tuples the agent emits on `view`, no open positions."""
    conn = init_db(":memory:")
    out = TrendBreakoutAgent(config={}, conn=conn).decide(view)
    return {(d.action, d.coin, d.side) for d in out}


def test_live_closes_loader_matches_backtest_frame_and_decisions():
    """Live deployment parity: the `build_closes_1h` loader must feed the agent the
    SAME 1h close series the backtester scores on, so the paper agent reproduces the
    G0-confirmed signal (B1d-trend-deploy Slice 2 — "evidence before capital").

    Construct one set of raw 1h candles, then derive `closes` two ways — the live
    path (`build_closes_1h`) and the backtest path (`build_frames` -> last Frame) —
    and assert (a) the per-coin series are byte-identical and (b) the agent emits the
    SAME entry/exit decisions on each. Series length (260) exceeds `closes_window`
    (240) so the window cap is actually exercised, and the off-by-one current-bar
    inclusion (both must end on the latest close) is what a wiring bug would break.
    Volume + mids are held constant across both views to isolate the closes series —
    the only input Slice 1 changed and the actual deployment risk.
    """
    from hl_bot.agents.base import MarketView
    from hl_bot.backtest.data import build_closes_1h, build_frames

    n = 260
    window = 240
    # BTC ramps to a new high on the last bar (long breakout); SOL ramps down to a
    # new low (short breakout); ETH is flat (no breakout) — covers entry + no-entry.
    rows = {
        "BTC": [{"t": i * HOUR, "c": 100.0 + i, "v": 1.0} for i in range(n)],
        "ETH": [{"t": i * HOUR, "c": 500.0, "v": 1.0} for i in range(n)],
        "SOL": [{"t": i * HOUR, "c": 1000.0 - i, "v": 1.0} for i in range(n)],
    }
    now_ms = (n - 1) * HOUR + HOUR // 2  # just after the latest bar opens

    # --- live path ---
    def post_fn(payload):
        req = payload["req"]
        coin = req["coin"]
        s, e = req["startTime"], req["endTime"]
        return [r for r in rows.get(coin, []) if s <= r["t"] <= e]

    live_closes = build_closes_1h(
        post_fn, ["BTC", "ETH", "SOL"], closes_window=window, now_ms=now_ms)

    # --- backtest path: closes_window matches the live loader's default (240) ---
    frames = build_frames(rows, vwap_window=10, closes_window=window, warmup=10)
    last = frames[-1]
    bt_closes = {k: list(v) for k, v in last.closes.items()}

    # (a) the two closes series are identical per coin
    assert set(live_closes) == set(bt_closes)
    for coin in bt_closes:
        assert live_closes[coin] == bt_closes[coin], f"{coin} closes diverge live vs sim"
        assert len(bt_closes[coin]) == window, "window cap exercised (series was longer)"
        assert bt_closes[coin][-1] == rows[coin][-1]["c"], "both end on the latest close"

    # (b) the agent makes the SAME decisions on each view (volume + mids held equal)
    vol = {c: 50_000_000.0 for c in rows}
    mids = dict(last.mids)
    live_view = MarketView(ts_ms=now_ms, mids=mids,
                           extra={"closes": live_closes, "day_ntl_vlm": vol})
    bt_view = MarketView(ts_ms=last.ts_ms, mids=mids,
                         extra={"closes": bt_closes, "day_ntl_vlm": vol})
    live_d = _decisions(live_view)
    bt_d = _decisions(bt_view)
    assert live_d == bt_d, f"live vs sim decisions diverge: {live_d} != {bt_d}"
    # and the signal actually fired (not a vacuous both-empty match)
    assert ("place", "BTC", "B") in live_d   # new-high long
    assert ("place", "SOL", "A") in live_d   # new-low short
