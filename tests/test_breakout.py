"""Tests for the breakout (Donchian channel momentum) agent — B-EDGE2.

Pure-function coverage for the channel math, decide() coverage for entries /
exits / guards via the audit-log replay, and an engine integration run proving
the agent rides a synthetic trend profitably net of costs (the property twap_mr
structurally cannot have).
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.breakout import BreakoutAgent, channel_break, channel_exit
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

MIN = 60_000
VOL = {"UP": 5e7}


def _view(ts_ms: int, mids: dict, closes: dict, vol: dict | None = None) -> MarketView:
    return MarketView(ts_ms=ts_ms, mids=mids,
                      extra={"closes": closes, "day_ntl_vlm": vol or {c: 5e7 for c in mids}})


# ---------------------------------------------------------------------------
# Pure channel math
# ---------------------------------------------------------------------------


def test_channel_break_directions_and_strength():
    closes = [100.0] * 10 + [101.0]  # current bar (101) breaks the 100-flat channel
    side, strength = channel_break(closes, 101.0, lookback=10, min_break_pct=0.0)
    assert side == "B" and abs(strength - 0.01) < 1e-9
    closes = [100.0] * 10 + [99.0]
    side, strength = channel_break(closes, 99.0, lookback=10, min_break_pct=0.0)
    assert side == "A" and abs(strength - 0.01) < 1e-9


def test_channel_break_inside_channel_and_buffer():
    closes = [100.0, 102.0, 98.0] * 4
    assert channel_break(closes, 101.0, 10, 0.0) == (None, 0.0)  # inside [98,102]
    # marginal break blocked by the pct buffer, passes without it
    closes = [100.0] * 10 + [100.4]
    assert channel_break(closes, 100.4, 10, 0.0)[0] == "B"
    assert channel_break(closes, 100.4, 10, 0.005) == (None, 0.0)


def test_channel_break_excludes_current_bar_and_needs_history():
    # The current bar's own high must not be part of the channel it breaks:
    # with only the spike itself in history, lookback+1 isn't met -> no signal.
    assert channel_break([105.0], 105.0, 10, 0.0) == (None, 0.0)
    assert channel_break([100.0] * 10, 101.0, 10, 0.0) == (None, 0.0)  # need 11
    # ...and a re-test of a level the CURRENT series already contains is not a
    # break: last close is in closes but excluded from the channel.
    closes = [100.0] * 10 + [105.0, 105.0]
    assert channel_break(closes, 105.0, 10, 0.0) == (None, 0.0)


def test_channel_exit_both_sides():
    closes = [100.0, 99.0, 101.0] * 3 + [98.0]
    assert channel_exit(closes, 98.0, is_long=True, exit_lookback=6)        # < min
    assert not channel_exit(closes, 100.0, is_long=True, exit_lookback=6)   # inside
    assert channel_exit(closes[:-1] + [102.0], 102.0, is_long=False, exit_lookback=6)
    assert not channel_exit([100.0, 101.0], 98.0, is_long=True, exit_lookback=6)  # short hist


# ---------------------------------------------------------------------------
# decide(): entries, guards
# ---------------------------------------------------------------------------


def _agent(cfg: dict | None = None, conn=None) -> BreakoutAgent:
    base = {"lookback_bars": 10, "exit_lookback_bars": 5}
    base.update(cfg or {})
    return BreakoutAgent(config=base, conn=conn or init_db(":memory:"))


def test_decide_enters_long_on_breakout():
    closes = {"UP": [100.0] * 12 + [102.0]}
    out = _agent().decide(_view(20 * MIN, {"UP": 102.0}, closes, VOL))
    places = [d for d in out if d.action == "place"]
    assert len(places) == 1 and places[0].coin == "UP" and places[0].side == "B"


def test_decide_enters_short_on_breakdown():
    closes = {"DN": [100.0] * 12 + [97.0]}
    out = _agent().decide(_view(20 * MIN, {"DN": 97.0}, closes))
    places = [d for d in out if d.action == "place"]
    assert len(places) == 1 and places[0].side == "A"


def test_decide_volume_floor_blocks_entry():
    closes = {"UP": [100.0] * 12 + [102.0]}
    out = _agent().decide(_view(20 * MIN, {"UP": 102.0}, closes, {"UP": 1e6}))
    assert all(d.action == "hold" for d in out)


def test_decide_ranks_by_break_strength_when_room_limited():
    closes = {
        "BIG": [100.0] * 12 + [105.0],    # +5% break
        "SMALL": [100.0] * 12 + [101.0],  # +1% break
    }
    out = _agent({"max_concurrent_positions": 1}).decide(
        _view(20 * MIN, {"BIG": 105.0, "SMALL": 101.0}, closes))
    places = [d for d in out if d.action == "place"]
    assert [d.coin for d in places] == ["BIG"]


def test_decide_cooldown_blocks_reentry_after_exit():
    conn = init_db(":memory:")
    # exited UP one minute ago; breakout still "live" on the stale channel
    log_decision(conn, Decision(agent="breakout_v1", action="place", coin="UP",
                                side="B", sz=1.0, px=100.0, reasoning="seed"))
    log_decision(conn, Decision(agent="breakout_v1", action="flatten", coin="UP",
                                side="A", sz=1.0, px=99.0, reasoning="seed"))
    row = conn.execute("SELECT MAX(ts_ms) FROM agent_decisions").fetchone()
    now_ms = int(row[0]) + MIN
    closes = {"UP": [100.0] * 12 + [102.0]}
    view = _view(now_ms, {"UP": 102.0}, closes, VOL)
    out = _agent({"reentry_cooldown_hours": 1.0}, conn).decide(view)
    assert all(d.action == "hold" for d in out)
    # with the cooldown off it re-enters
    out = _agent({"reentry_cooldown_hours": 0.0}, conn).decide(view)
    assert any(d.action == "place" and d.coin == "UP" for d in out)


# ---------------------------------------------------------------------------
# decide(): exits (seeded open position in the audit log)
# ---------------------------------------------------------------------------


def _seed_long(conn, coin: str, px: float) -> None:
    log_decision(conn, Decision(agent="breakout_v1", action="place", coin=coin,
                                side="B", sz=1.0, px=px, reasoning="seed"))


def test_decide_stop_loss_exit():
    conn = init_db(":memory:")
    _seed_long(conn, "UP", 100.0)
    closes = {"UP": [100.0] * 13}  # no channel exit signal
    out = _agent({"stop_loss_pct": 0.03}, conn).decide(
        _view(10 * MIN, {"UP": 96.0}, closes, VOL))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "STOP" in flats[0].reasoning


def test_decide_channel_exit_long():
    conn = init_db(":memory:")
    _seed_long(conn, "UP", 100.0)
    closes = {"UP": [100.0, 99.5, 100.5] * 3 + [99.0]}  # mid < prior 5-bar min
    out = _agent(conn=conn).decide(_view(10 * MIN, {"UP": 99.0}, closes, VOL))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "CHANNEL-EXIT" in flats[0].reasoning


def test_decide_holds_winner_inside_channel():
    conn = init_db(":memory:")
    _seed_long(conn, "UP", 100.0)
    closes = {"UP": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]}
    out = _agent(conn=conn).decide(_view(10 * MIN, {"UP": 105.0}, closes, VOL))
    assert not [d for d in out if d.action == "flatten"]


# ---------------------------------------------------------------------------
# closes_key: the live roster feeds 15m closes under a different view key
# ---------------------------------------------------------------------------


def test_decide_closes_key_routes_entry_feed():
    breakout = [100.0] * 12 + [102.0]
    view = MarketView(ts_ms=20 * MIN, mids={"UP": 102.0},
                      extra={"closes": {}, "closes_15m": {"UP": breakout},
                             "day_ntl_vlm": VOL})
    out = _agent({"closes_key": "closes_15m"}).decide(view)
    assert any(d.action == "place" and d.coin == "UP" for d in out)
    # default-key agent on the same view sees no closes -> holds
    out = _agent().decide(view)
    assert all(d.action == "hold" for d in out)


def test_decide_closes_key_routes_exit_feed():
    conn = init_db(":memory:")
    _seed_long(conn, "UP", 100.0)
    exit_pattern = [100.0, 99.5, 100.5] * 3 + [99.0]  # mid < prior 5-bar min
    view = MarketView(ts_ms=10 * MIN, mids={"UP": 99.0},
                      extra={"closes": {}, "closes_15m": {"UP": exit_pattern},
                             "day_ntl_vlm": VOL})
    out = _agent({"closes_key": "closes_15m"}, conn).decide(view)
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "CHANNEL-EXIT" in flats[0].reasoning


# ---------------------------------------------------------------------------
# Engine integration: trend pays, chop doesn't trade
# ---------------------------------------------------------------------------


def _frames_from_prices(prices: list[float], window: int = 11) -> list[Frame]:
    frames = []
    for i in range(1, len(prices)):
        closes = prices[max(0, i + 1 - window):i + 1]
        frames.append(Frame(
            ts_ms=i * MIN, mids={"UP": prices[i]},
            day_ntl_vlm={"UP": 5e7}, closes={"UP": closes},
        ))
    return frames


def test_backtest_breakout_profits_on_trend():
    # flat base, then a steady +0.4%/bar trend: one long entry near the start
    # of the move, riding it, liquidated at the end -> positive net of costs.
    prices = [100.0] * 15 + [100.0 * 1.004 ** i for i in range(1, 41)]
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = BreakoutAgent(config={"lookback_bars": 10, "exit_lookback_bars": 5},
                          conn=conn)
    res = bt.run(agent, _frames_from_prices(prices))
    assert res.net_pnl > 0
    assert res.scorecard.n_trades >= 2  # entry + (channel exit or liquidate)


def test_backtest_breakout_stays_flat_in_chop():
    prices = [100.0, 100.5, 99.5] * 20
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = BreakoutAgent(config={"lookback_bars": 10, "exit_lookback_bars": 5},
                          conn=conn)
    res = bt.run(agent, _frames_from_prices(prices))
    assert res.scorecard.n_trades == 0


def test_backtest_factory_registered():
    from hl_bot.cli.main import _backtest_factories

    factories = _backtest_factories({"lookback_bars": 7})
    agent = factories["breakout_v1"](init_db(":memory:"))
    assert isinstance(agent, BreakoutAgent)
    assert agent.cfg.lookback_bars == 7
