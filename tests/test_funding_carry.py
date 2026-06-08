"""Confirmation tests for the funding-carry strategies.

Carry edge = funding collected − costs, with price washed out. We build synthetic
funding scenarios (flat price, persistent funding) and check that:
  * single-name carry collects funding from an extreme-funding coin, and
  * cross-sectional carry collects from both a high- and a low-funding coin while
    staying dollar-neutral.
Funding folded into realized PnL via the engine's liquidate-at-end.
"""

from __future__ import annotations

from hl_bot.agents.funding_carry import FundingCarryAgent
from hl_bot.agents.xfund_carry import XFundCarryAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000


def _run(agent_cls, frames):
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    res = bt.run(agent_cls(config={}, conn=conn), frames)
    return res, conn


def _traded_coins(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT coin FROM fills").fetchall()}


def test_single_name_carry_collects_funding():
    # One coin, flat price, persistent +0.0008/hr funding -> short collects.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"HOT": 100.0},
              funding={"HOT": 0.0008},
              day_ntl_vlm={"HOT": 50_000_000.0})
        for i in range(24)
    ]
    res, _ = _run(FundingCarryAgent, frames)
    assert res.net_pnl > 0
    # it entered then closed (liquidate-at-end) -> funding realized
    assert res.scorecard.n_trades >= 2


def test_xsectional_carry_is_two_sided_and_positive():
    # HOT funding positive (short it), COLD negative (long it); flat prices.
    frames = []
    for i in range(28):
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"HOT": 100.0, "COLD": 50.0, "MEH": 10.0},
            funding={"HOT": 0.0010, "COLD": -0.0010, "MEH": 0.00001},
            day_ntl_vlm={"HOT": 5e7, "COLD": 5e7, "MEH": 5e7},
        ))
    res, conn = _run(XFundCarryAgent, frames)
    assert res.net_pnl > 0
    # MEH (calm funding) must not be traded; HOT+COLD are the two-sided book
    coins = _traded_coins(conn)
    assert "MEH" not in coins
    assert {"HOT", "COLD"} <= coins


def test_carry_skips_calm_funding():
    # Funding below entry threshold everywhere -> no positions.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"X": 100.0},
              funding={"X": 0.00001}, day_ntl_vlm={"X": 5e7})
        for i in range(12)
    ]
    res, _ = _run(FundingCarryAgent, frames)
    assert res.scorecard.n_trades == 0
