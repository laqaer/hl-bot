"""Confirmation tests for the funding-carry strategies.

Carry edge = funding collected − costs, with price washed out. We build synthetic
funding scenarios (flat price, persistent funding) and check that:
  * single-name carry collects funding from an extreme-funding coin, and
  * cross-sectional carry collects from both a high- and a low-funding coin while
    staying dollar-neutral.
Funding folded into realized PnL via the engine's liquidate-at-end.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision, log_decision
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


def _seed_short(conn, agent: str, coin: str, sz: float, px: float) -> None:
    log_decision(conn, Decision(agent=agent, action="place", coin=coin,
                                side="A", sz=sz, px=px, reasoning="seed"))


def test_hold_while_eligible_keeps_rank_rotated_leg():
    # HELD short on HOT2 (+0.0005/hr, still eligible) but it rotated out of the
    # top-2 because HOTA/HOTB now carry MORE funding. Default mode kicks it out
    # ("DROPPED from carry set"); hold_while_eligible keeps it (no churn).
    view = MarketView(
        ts_ms=10 * HOUR,
        mids={"HOT2": 100.0, "HOTA": 100.0, "HOTB": 100.0},
        funding={"HOT2": 0.0005, "HOTA": 0.0010, "HOTB": 0.0008},
        extra={"day_ntl_vlm": {"HOT2": 5e7, "HOTA": 5e7, "HOTB": 5e7}},
    )

    conn_default = init_db(":memory:")
    _seed_short(conn_default, "xfund_carry_v1", "HOT2", 0.25, 100.0)
    out_default = XFundCarryAgent(config={}, conn=conn_default).decide(view)
    assert any(d.action == "flatten" and d.coin == "HOT2" for d in out_default)

    conn_hold = init_db(":memory:")
    _seed_short(conn_hold, "xfund_carry_v1", "HOT2", 0.25, 100.0)
    out_hold = XFundCarryAgent(
        config={"hold_while_eligible": True}, conn=conn_hold
    ).decide(view)
    assert not any(d.action == "flatten" and d.coin == "HOT2" for d in out_hold)
    # ...and it is not re-entered (still an active leg), so no duplicate place.
    assert not any(d.action == "place" and d.coin == "HOT2" for d in out_hold)


def test_hold_while_eligible_still_exits_on_flip_and_normalize():
    # Even in hold mode, a held leg exits when its funding flips sign or normalizes.
    view = MarketView(
        ts_ms=10 * HOUR,
        mids={"FLIP": 100.0, "CALM": 100.0},
        funding={"FLIP": -0.0009, "CALM": 0.00001},  # FLIP now long-side; CALM ~0
        extra={"day_ntl_vlm": {"FLIP": 5e7, "CALM": 5e7}},
    )
    conn = init_db(":memory:")
    _seed_short(conn, "xfund_carry_v1", "FLIP", 0.25, 100.0)  # held SHORT
    _seed_short(conn, "xfund_carry_v1", "CALM", 0.25, 100.0)  # held SHORT
    out = XFundCarryAgent(config={"hold_while_eligible": True}, conn=conn).decide(view)
    reasons = {d.coin: d.reasoning for d in out if d.action == "flatten"}
    assert "FLIP" in reasons and "FLIPPED" in reasons["FLIP"]
    assert "CALM" in reasons and "NORMALIZED" in reasons["CALM"]


def test_carry_skips_calm_funding():
    # Funding below entry threshold everywhere -> no positions.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"X": 100.0},
              funding={"X": 0.00001}, day_ntl_vlm={"X": 5e7})
        for i in range(12)
    ]
    res, _ = _run(FundingCarryAgent, frames)
    assert res.scorecard.n_trades == 0
