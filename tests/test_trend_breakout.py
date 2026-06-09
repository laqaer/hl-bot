"""Evidence tests for trend_breakout_v1.

The trend-follow thesis is the mirror of TWAP-MR: ride a breakout instead of
fading it. So the tests that matter are directional — on a sustained uptrend the
agent goes LONG and profits (where the fader bleeds), on a sustained downtrend it
goes SHORT, and a Donchian reversal flips it back out.
"""

from __future__ import annotations

from hl_bot.agents.trend_breakout import TrendBreakoutAgent
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


def test_trailing_channel_exits_on_reversal():
    # Rip up (enter long), then sharp reversal below the exit channel -> flatten.
    up = [100.0] * 6 + [100.0 + 2.0 * i for i in range(1, 8)]   # breakout long
    down = [up[-1] - 4.0 * i for i in range(1, 6)]              # reversal
    frames = _frames_from_closes(up + down)
    _, conn = _run(TrendBreakoutAgent, frames, CFG)
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM agent_decisions WHERE action IN ('place','flatten') ORDER BY ts_ms").fetchall()]
    assert "place" in actions and "flatten" in actions
