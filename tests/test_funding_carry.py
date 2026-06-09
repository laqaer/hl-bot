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


def _closes_from_returns(rets: list[float], base: float = 100.0) -> list[float]:
    out = [base]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out

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
    # coin returns are exactly 2x the market's -> OLS beta == 2.0 (mean-invariant).
    mkt_rets = [0.01, -0.02, 0.03, -0.01, 0.02, -0.015, 0.025]
    mkt = _closes_from_returns(mkt_rets)
    coin = _closes_from_returns([2 * r for r in mkt_rets])
    b = rolling_beta(coin, mkt)
    assert b is not None
    assert abs(b - 2.0) < 1e-9


def test_rolling_beta_none_on_degenerate_input():
    assert rolling_beta([100.0, 101.0], [100.0, 101.0]) is None  # too few returns
    assert rolling_beta([100, 101, 102, 103], [100, 100, 100, 100]) is None  # flat mkt


def test_beta_neutral_shrinks_the_higher_beta_leg():
    # SHORT HOT (+funding, beta 1.5) vs LONG COLD (-funding, beta 0.5). Dollar-
    # neutral sizes them equally; beta-neutral shrinks the high-beta short so the
    # book's net market exposure cancels.
    base_rets = [0.01, -0.02, 0.03, -0.01, 0.02, -0.015, 0.025, -0.005]
    btc = _closes_from_returns(base_rets)
    hot = _closes_from_returns([1.5 * r for r in base_rets])
    cold = _closes_from_returns([0.5 * r for r in base_rets])
    view = MarketView(
        ts_ms=10 * HOUR,
        mids={"HOT": 100.0, "COLD": 100.0, "BTC": 100.0},
        funding={"HOT": 0.0010, "COLD": -0.0010, "BTC": 0.0},
        extra={
            "day_ntl_vlm": {"HOT": 5e7, "COLD": 5e7, "BTC": 5e7},
            "closes": {"HOT": hot, "COLD": cold, "BTC": btc},
        },
    )

    def _sizes(cfg):
        conn = init_db(":memory:")
        out = XFundCarryAgent(config=cfg, conn=conn).decide(view)
        return {d.coin: d.sz for d in out if d.action == "place"}

    base = _sizes({})
    bn = _sizes({"beta_neutral": True})
    # dollar-neutral: equal sizes on both legs
    assert abs(base["HOT"] - base["COLD"]) < 1e-9
    # beta-neutral: high-beta short leg is shrunk vs the low-beta long leg...
    assert bn["HOT"] < bn["COLD"]
    # ...the low-beta leg keeps full size (scale ref/ref = 1.0)...
    assert abs(bn["COLD"] - base["COLD"]) < 1e-9
    # ...and the shrink ratio tracks the beta ratio (0.5/1.5) up to sz rounding.
    assert abs(bn["HOT"] / bn["COLD"] - (0.5 / 1.5)) < 1e-3


def test_carry_skips_calm_funding():
    # Funding below entry threshold everywhere -> no positions.
    frames = [
        Frame(ts_ms=i * HOUR, mids={"X": 100.0},
              funding={"X": 0.00001}, day_ntl_vlm={"X": 5e7})
        for i in range(12)
    ]
    res, _ = _run(FundingCarryAgent, frames)
    assert res.scorecard.n_trades == 0


def _entries(out) -> dict[str, str]:
    return {d.coin: d.side for d in out if d.action == "place"}


def test_per_hour_funding_thresholds_are_interval_invariant():
    # The enter/exit thresholds are per-HOUR, but the data layer scales
    # Frame.funding by bar length (4h bar -> 4x the hourly rate). The agent must
    # normalize by extra["bar_hours"] so the SAME config picks the same legs at
    # any interval. MEH's hourly rate (0.00003) is below the 0.0001 entry
    # threshold; without normalization its 4h-scaled value (0.00012) would clear
    # it and MEH would be wrongly entered.
    vol = {"day_ntl_vlm": {"HOT": 5e7, "COLD": 5e7, "MEH": 5e7}}
    v1 = MarketView(
        ts_ms=HOUR, mids={"HOT": 100.0, "COLD": 50.0, "MEH": 10.0},
        funding={"HOT": 0.0002, "COLD": -0.0002, "MEH": 0.00003},
        extra={"bar_hours": 1.0, **vol},
    )
    v4 = MarketView(  # same per-hour rates, scaled by bar_hours=4 as data.py does
        ts_ms=HOUR, mids={"HOT": 100.0, "COLD": 50.0, "MEH": 10.0},
        funding={"HOT": 0.0008, "COLD": -0.0008, "MEH": 0.00012},
        extra={"bar_hours": 4.0, **vol},
    )
    e1 = _entries(XFundCarryAgent(config={}, conn=init_db(":memory:")).decide(v1))
    e4 = _entries(XFundCarryAgent(config={}, conn=init_db(":memory:")).decide(v4))
    assert e1 == {"HOT": "A", "COLD": "B"}      # short the +funding, long the -funding
    assert e4 == e1                              # interval-invariant: MEH skipped at 4h too


def test_rebalance_cooldown_gates_entries_but_not_derisk_exits():
    # With rebalance_hours=4, a book change at T should freeze new entries and
    # rank-rotation churn within 4h, while still allowing a risk-reducing exit
    # (funding flipped to the wrong side). After 4h, the agent rebalances again.
    from hl_bot.backtest.engine import frozen_clock

    cfg = {"rebalance_hours": 4.0}
    base_t = 1_000 * HOUR

    def _decide_at(hours_after: float, view: MarketView):
        conn = init_db(":memory:")
        with frozen_clock((base_t) / 1000.0):
            _seed_short(conn, "xfund_carry_v1", "OLD", 0.25, 100.0)  # held SHORT @ T
        with frozen_clock((base_t + hours_after * HOUR) / 1000.0):
            return XFundCarryAgent(config=cfg, conn=conn).decide(view)

    # OLD's funding flipped negative (wrong side -> must de-risk); NEW carries
    # strong +funding (a candidate new short).
    view = MarketView(
        ts_ms=base_t,
        mids={"OLD": 100.0, "NEW": 100.0},
        funding={"OLD": -0.0009, "NEW": 0.0009},
        extra={"bar_hours": 1.0, "day_ntl_vlm": {"OLD": 5e7, "NEW": 5e7}},
    )

    within = _decide_at(1.0, view)   # 1h after the seed -> inside the 4h cooldown
    assert any(d.action == "flatten" and d.coin == "OLD" for d in within)  # de-risk fires
    assert not any(d.action == "place" for d in within)                    # entries frozen

    after = _decide_at(5.0, view)    # 5h after the seed -> cooldown elapsed
    assert any(d.action == "flatten" and d.coin == "OLD" for d in after)
    assert any(d.action == "place" and d.coin == "NEW" for d in after)     # rebalanced
