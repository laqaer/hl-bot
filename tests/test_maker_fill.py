"""Resting maker-fill realism (CostModel.maker_fill='resting').

The optimistic maker mode fills every order instantly at mid — an upper bound.
These tests pin the honest mode to the live maker lifecycle (exec/maker.py):
entries rest as post-only limits and fill only when a later bar's mid trades
strictly through the limit; stale quotes cancel after the TTL; one working
quote per coin; exits pay full taker fee + slippage.
"""

from __future__ import annotations

from hl_bot.agents.base import Agent, MarketView
from hl_bot.agents.decisions import Decision
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

MIN = 60_000  # 1m bars: well inside the 1800s TTL


def _frame(i: int, mid: float, *, coin: str = "TST", step_ms: int = MIN) -> Frame:
    return Frame(ts_ms=i * step_ms, mids={coin: mid})


class ScriptedAgent(Agent):
    """Emits a fixed decision list per frame index — full control, no signal logic."""

    def __init__(self, script: dict[int, list[Decision]], name: str = "scripted") -> None:
        super().__init__(name)
        self.script = script
        self.i = 0

    def decide(self, view: MarketView) -> list[Decision]:
        out = self.script.get(self.i, [])
        self.i += 1
        return out


def _place(side: str = "B", sz: float = 1.0, coin: str = "TST") -> Decision:
    return Decision(agent="scripted", action="place", coin=coin, side=side, sz=sz)


def _flatten(sz: float = 1.0, coin: str = "TST") -> Decision:
    return Decision(agent="scripted", action="flatten", coin=coin, sz=sz)


def _resting_cost(**kw) -> CostModel:
    return CostModel(maker=True, maker_fill="resting", **kw)


def test_buy_quote_fills_only_when_mid_crosses_below_limit():
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    # quote at 100; bar1 stays above the limit -> no fill; bar2 trades through.
    frames = [_frame(0, 100.0), _frame(1, 100.5), _frame(2, 99.0)]
    res = bt.run(agent, frames, liquidate_at_end=False)

    fills = conn.execute("SELECT px, fee, side FROM fills").fetchall()
    assert len(fills) == 1
    px, fee, side = fills[0]
    assert px == 100.0                       # filled AT the limit, not bar2's 99
    assert side == "B"
    assert abs(fee - 100.0 * 1.0 * 1.0 / 10_000) < 1e-12   # maker 1bp, zero slip
    assert res.maker_fill_stats == {"rested": 1, "filled": 1, "expired": 0}
    assert "TST" in bt._book and bt._book["TST"].entry_px == 100.0


def test_no_cross_means_no_fill_and_no_position():
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    # price never comes back to the 100 limit
    frames = [_frame(0, 100.0), _frame(1, 100.2), _frame(2, 101.0)]
    res = bt.run(agent, frames, liquidate_at_end=False)

    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert not bt._book
    # end-of-run cancel counts as expired
    assert res.maker_fill_stats == {"rested": 1, "filled": 0, "expired": 1}


def test_equality_at_limit_does_not_fill():
    """Mid touching the limit exactly = unknowable queue position -> no fill."""
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    frames = [_frame(0, 100.0), _frame(1, 100.0), _frame(2, 100.0)]
    bt.run(agent, frames, liquidate_at_end=False)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_sell_quote_fills_when_mid_crosses_above():
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("A")]})
    frames = [_frame(0, 100.0), _frame(1, 101.0)]
    bt.run(agent, frames, liquidate_at_end=False)

    fills = conn.execute("SELECT px, side FROM fills").fetchall()
    assert len(fills) == 1
    assert fills[0]["px"] == 100.0 and fills[0]["side"] == "A"
    assert bt._book["TST"].side == "A"


def test_stale_quote_cancels_after_ttl_even_if_price_later_crosses():
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    # hourly bars: bar1 is 3600s after the quote -> past the 1800s TTL.
    # bar2 crosses the limit but the quote is already cancelled.
    frames = [
        _frame(0, 100.0, step_ms=3_600_000),
        _frame(1, 100.5, step_ms=3_600_000),
        _frame(2, 99.0, step_ms=3_600_000),
    ]
    res = bt.run(agent, frames, liquidate_at_end=False)

    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert res.maker_fill_stats == {"rested": 1, "filled": 0, "expired": 1}


def test_stale_check_wins_over_same_bar_cross():
    """A bar that is both past-TTL and crossing does NOT fill (cancel-first)."""
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    frames = [_frame(0, 100.0, step_ms=3_600_000), _frame(1, 99.0, step_ms=3_600_000)]
    res = bt.run(agent, frames, liquidate_at_end=False)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert res.maker_fill_stats["expired"] == 1


def test_one_working_quote_per_coin():
    """A re-signal while a quote rests is dropped (live has_resting_order)."""
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B", sz=1.0)], 1: [_place("B", sz=5.0)]})
    frames = [_frame(0, 100.0), _frame(1, 100.5), _frame(2, 99.0)]
    res = bt.run(agent, frames, liquidate_at_end=False)

    fills = conn.execute("SELECT sz FROM fills").fetchall()
    assert len(fills) == 1
    assert fills[0]["sz"] == 1.0            # the original quote, not the re-signal
    assert res.maker_fill_stats["rested"] == 1


def test_exits_pay_taker_in_resting_mode():
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    agent = ScriptedAgent({0: [_place("B")], 2: [_flatten()]})
    frames = [_frame(0, 100.0), _frame(1, 99.0), _frame(2, 102.0)]
    bt.run(agent, frames, liquidate_at_end=False)

    rows = conn.execute("SELECT px, fee, side FROM fills ORDER BY rowid").fetchall()
    assert len(rows) == 2
    entry, exit_ = rows
    assert entry["px"] == 100.0
    # close of a long hits the bid: taker slippage below mid + taker fee
    expected_exit_px = 102.0 * (1 - 2.0 / 10_000)
    assert abs(exit_["px"] - expected_exit_px) < 1e-9
    assert abs(exit_["fee"] - expected_exit_px * 1.0 * 4.5 / 10_000) < 1e-12


def test_filled_position_visible_to_agent_same_bar():
    """Fills are processed before decide(), so the agent can exit on the fill bar."""
    conn = init_db(":memory:")
    bt = Backtester(_resting_cost(), conn=conn)
    # bar1's decide flattens — legal only if the bar1 fill landed before decide.
    agent = ScriptedAgent({0: [_place("B")], 1: [_flatten()]})
    frames = [_frame(0, 100.0), _frame(1, 99.0)]
    bt.run(agent, frames, liquidate_at_end=False)
    sides = [r["side"] for r in conn.execute("SELECT side FROM fills ORDER BY rowid")]
    assert sides == ["B", "A"]
    assert not bt._book


def test_optimistic_default_unchanged():
    """maker_fill defaults to optimistic: instant fill at mid, both legs maker."""
    cost = CostModel(maker=True)
    assert cost.maker_fill == "optimistic" and not cost.resting
    assert cost.exit_fee_rate == cost.fee_rate and cost.exit_slip == cost.slip

    conn = init_db(":memory:")
    bt = Backtester(cost, conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    res = bt.run(agent, [_frame(0, 100.0)], liquidate_at_end=False)
    fills = conn.execute("SELECT px FROM fills").fetchall()
    assert len(fills) == 1 and fills[0]["px"] == 100.0
    assert res.maker_fill_stats is None      # stats only reported in resting mode


def test_taker_mode_ignores_maker_fill():
    cost = CostModel(maker=False, maker_fill="resting")
    assert not cost.resting
    conn = init_db(":memory:")
    bt = Backtester(cost, conn=conn)
    agent = ScriptedAgent({0: [_place("B")]})
    bt.run(agent, [_frame(0, 100.0)], liquidate_at_end=False)
    fills = conn.execute("SELECT px FROM fills").fetchall()
    assert len(fills) == 1                   # instant taker fill, nothing rests
    assert fills[0]["px"] == 100.0 * (1 + 2.0 / 10_000)


def test_bad_maker_fill_value_rejected():
    import pytest

    with pytest.raises(ValueError):
        CostModel(maker_fill="hopeful")


def test_confirm_threads_maker_fill():
    """The G0 harness reprices its maker arm with the requested fill model."""
    from hl_bot.backtest.confirm import confirm_strategy

    def factory(conn):
        # fresh per-scenario agent: quote bar0, exit when filled
        return ScriptedAgent({0: [_place("B")], 5: [_flatten()]})

    frames = [_frame(i, 100.0 + (-1.0 if i == 3 else 0.0)) for i in range(12)]
    res = confirm_strategy(factory, frames, prefer="maker", maker_fill="resting",
                           min_trades=1)
    names = [s.name for s in res.cost_ladder]
    assert "maker-rest" in names
    assert res.in_sample.name == "in-sample(maker-rest)"
