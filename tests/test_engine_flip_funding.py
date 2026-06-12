"""Regression tests for two backtest-engine correctness bugs (Codex review):

  1. An opposite-side `place` that reverses a position must open only the
     *remainder* (not the full order size) and must not double-count fees/notional.
  2. Funding must be scaled to the bar interval, not applied as a raw hourly rate
     on every bar regardless of cadence.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import Agent, MarketView
from hl_bot.backtest.data import build_frames
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000
COIN = "TST"


class _Scripted(Agent):
    """Emits a preset list of decisions per tick (for engine-level tests)."""

    def __init__(self, conn, script):
        super().__init__("scripted_v1", {})
        self.conn = conn
        self.script = script
        self.i = 0

    def decide(self, view: MarketView):
        out = self.script[self.i] if self.i < len(self.script) else []
        self.i += 1
        return out


def _place(side, sz):
    from hl_bot.agents.decisions import Decision
    return Decision(agent="scripted_v1", action="place", coin=COIN, side=side,
                    sz=sz, px=100.0, cloid=None)


def test_opposite_side_place_opens_only_remainder():
    # long 5, then short 7 -> should end SHORT 2, not short 7.
    frames = [Frame(ts_ms=i * HOUR, mids={COIN: 100.0}) for i in range(3)]
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn)
    agent = _Scripted(conn, [[_place("B", 5.0)], [_place("A", 7.0)], []])
    res = bt.run(agent, frames)  # liquidate_at_end closes the remaining 2

    # The flip's OPEN leg must be sized 2 (the remainder), not 7.
    open_short = conn.execute(
        "SELECT sz FROM agent_decisions WHERE action='place' AND side='A'"
    ).fetchone()
    assert open_short is not None
    assert open_short[0] == 2.0

    # No double-count: traded notional = 5 (open) + 5 (close) + 2 (open) + 2 (close) = 14 units.
    assert res.scorecard.notional_traded == pytest.approx(14.0 * 100.0, rel=2e-4)  # exits pay taker slip now
    assert res.scorecard.n_fills == 4   # n_trades counts nonzero closes only
    # No price move, no cost -> flat PnL (would be nonzero if sizing/fees were wrong).
    # Exits always pay taker (fee + slip) even in maker mode — matching
    # production market closes. Close legs: 5u + 2u @ ~100 = 700 notional.
    expected_cost = 700.0 * (4.5 + 2.0) / 10_000
    assert res.net_pnl == pytest.approx(-expected_cost, rel=2e-3)


def test_funding_scaled_by_bar_interval():
    # Same hourly funding rows; 5m bars must accrue 1/12 of the hourly rate/bar.
    candles = [{"t": i * 300_000, "c": 100.0, "v": 1.0} for i in range(40)]
    funding = [{"time": 0, "fundingRate": 0.0012}]
    f1h = build_frames({COIN: candles}, funding_by_coin={COIN: funding},
                       warmup=1, vwap_window=10, bar_hours=1.0)
    f5m = build_frames({COIN: candles}, funding_by_coin={COIN: funding},
                       warmup=1, vwap_window=10, bar_hours=5 / 60)
    assert f1h[-1].funding[COIN] == 0.0012
    assert abs(f5m[-1].funding[COIN] - 0.0012 * (5 / 60)) < 1e-12
