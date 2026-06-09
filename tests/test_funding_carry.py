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
from hl_bot.agents.xfund_carry import XFundCarryAgent, rolling_beta
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


def test_rolling_beta_recovers_known_slope():
    # proxy random-ish walk; coin = 2x proxy returns -> beta ~ 2.0
    proxy = [100.0]
    for r in [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.008, -0.012]:
        proxy.append(proxy[-1] * (1 + r))
    coin = [50.0]
    for i in range(1, len(proxy)):
        rp = proxy[i] / proxy[i - 1] - 1.0
        coin.append(coin[-1] * (1 + 2.0 * rp))
    b = rolling_beta(coin, proxy, window=48)
    assert b is not None and abs(b - 2.0) < 1e-6
    # proxy on itself is beta 1.0
    assert abs(rolling_beta(proxy, proxy, 48) - 1.0) < 1e-9
    # too little / degenerate data -> None
    assert rolling_beta([1.0, 2.0], [1.0, 2.0], 48) is None
    assert rolling_beta([1.0, 1.1, 1.2, 1.3], [5.0, 5.0, 5.0, 5.0], 48) is None


def test_beta_neutral_shrinks_high_beta_leg_relative_to_low_beta():
    # HIGH (short, beta 2) and LOW (long, beta 0.5) both eligible by funding.
    # Beta-neutral sizing must give the high-beta short LESS notional than the
    # low-beta long; plain dollar-neutral gives them equal notional.
    proxy = [100.0]
    for r in [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.008, -0.012, 0.006, -0.009]:
        proxy.append(proxy[-1] * (1 + r))

    def _scaled(mult):
        s = [10.0]
        for i in range(1, len(proxy)):
            rp = proxy[i] / proxy[i - 1] - 1.0
            s.append(s[-1] * (1 + mult * rp))
        return s

    closes = {"BTC": proxy, "HIGH": _scaled(2.0), "LOW": _scaled(0.5)}
    view = MarketView(
        ts_ms=20 * HOUR,
        mids={"BTC": proxy[-1], "HIGH": closes["HIGH"][-1], "LOW": closes["LOW"][-1]},
        funding={"BTC": 0.00001, "HIGH": 0.0010, "LOW": -0.0010},
        extra={"day_ntl_vlm": {"BTC": 5e7, "HIGH": 5e7, "LOW": 5e7}, "closes": closes},
    )

    def _notionals(cfg):
        conn = init_db(":memory:")
        out = XFundCarryAgent(config=cfg, conn=conn).decide(view)
        return {d.coin: d.sz * d.px for d in out if d.action == "place"}

    plain = _notionals({})
    assert abs(plain["HIGH"] - plain["LOW"]) < 1e-6  # dollar-neutral: equal

    bn = _notionals({"beta_neutral": True, "beta_floor": 0.5})
    # high-beta short shrunk to ~floor/2 of base; low-beta long stays at base.
    assert bn["HIGH"] < bn["LOW"]
    assert bn["HIGH"] < plain["HIGH"]


def test_carry_skips_calm_funding():
    # Funding below entry threshold everywhere -> no positions.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"X": 100.0},
              funding={"X": 0.00001}, day_ntl_vlm={"X": 5e7})
        for i in range(12)
    ]
    res, _ = _run(FundingCarryAgent, frames)
    assert res.scorecard.n_trades == 0
