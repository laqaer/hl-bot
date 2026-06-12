"""Per-agent funding attribution + Sharpe (B6/B7).

Carry strategies earn funding, not price PnL — so a scorecard that ignores
funding (or only computes Sharpe for the account) judges them on the wrong
numbers, which is exactly what the promotion/confirm gates key off. These tests
pin the fix: funding is attributed to the agent holding the coin at the time, and
per-agent Sharpe is computed from daily PnL.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.metrics import _agent_funding_payments, score_agent

DAY = 86_400_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "a.sqlite")


def _decision(conn, agent, action, coin, t_ms):
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, coin, is_paper) VALUES(?,?,?,?,0)",
        (t_ms, agent, action, coin),
    )


def _funding(conn, coin, t_ms, usdc):
    conn.execute(
        "INSERT INTO funding_payments(time_ms, coin, usdc, szi, funding_rate, raw_json) VALUES(?,?,?,?,?,?)",
        (t_ms, coin, usdc, 0.0, 0.0, "{}"),
    )


def _fill(conn, agent, coin, t_ms, pnl, fee=0.05, sz=1.0, px=100.0, side="B"):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{coin}{t_ms}", t_ms, t_ms, coin, side, px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_funding_attributed_to_holder(conn):
    now = int(time.time() * 1000)
    # agent A holds BTC from t-10m to t-1m; funding lands at t-5m -> attributed to A.
    _decision(conn, "funding_carry_v1", "place", "BTC", now - 600_000)
    _decision(conn, "funding_carry_v1", "flatten", "BTC", now - 60_000)
    _funding(conn, "BTC", now - 300_000, 2.50)

    pays = _agent_funding_payments(conn, "funding_carry_v1", now - DAY)
    assert sum(s for _, s in pays) == pytest.approx(2.50)
    # an unrelated agent gets none
    assert _agent_funding_payments(conn, "twap_mr_v1", now - DAY) == []

    sc = score_agent(conn, "funding_carry_v1", "24h")
    assert sc.funding_pnl == pytest.approx(2.50)
    assert sc.net_pnl == pytest.approx(2.50)  # no fills yet -> funding is the net


def test_funding_split_between_concurrent_holders(conn):
    now = int(time.time() * 1000)
    for ag in ("xfund_carry_v1", "funding_carry_v1"):
        _decision(conn, ag, "place", "ETH", now - 600_000)
        _decision(conn, ag, "flatten", "ETH", now - 60_000)
    _funding(conn, "ETH", now - 300_000, 4.00)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    assert a == pytest.approx(2.0) and b == pytest.approx(2.0)  # split, no double-count


def test_funding_split_by_held_size_when_fills_exist(conn):
    now = int(time.time() * 1000)
    # Two agents hold BTC at different sizes (real fills): A long 3, B long 1.
    _fill(conn, "xfund_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=3.0)
    _fill(conn, "funding_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=1.0)
    _funding(conn, "BTC", now - 300_000, 4.0)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    # 4.0 split 3:1 by size — not 2:2 — proving size-weighting beats equal-split.
    assert a == pytest.approx(3.0)
    assert b == pytest.approx(1.0)
    assert a + b == pytest.approx(4.0)  # conserves the account total


def test_funding_attribution_handles_offsetting_hedge(conn):
    now = int(time.time() * 1000)
    # A long 3, B short 1 -> account net = +2; funding is paid on the net.
    _fill(conn, "xfund_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=3.0, side="B")
    _fill(conn, "funding_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=1.0, side="A")
    _funding(conn, "BTC", now - 300_000, 2.0)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    assert a == pytest.approx(3.0)   # 2.0 * (+3 / +2)
    assert b == pytest.approx(-1.0)  # 2.0 * (-1 / +2): the hedge collects
    assert a + b == pytest.approx(2.0)  # signed shares still sum to the total


def test_funding_after_fill_close_not_attributed(conn):
    now = int(time.time() * 1000)
    # Open long 2 then fully close (sell 2) before funding -> flat, gets nothing.
    _fill(conn, "funding_carry_v1", "SOL", now - 600_000, pnl=0.0, fee=0.0, sz=2.0, side="B")
    _fill(conn, "funding_carry_v1", "SOL", now - 500_000, pnl=0.0, fee=0.0, sz=2.0, side="A")
    _funding(conn, "SOL", now - 100_000, 1.0)
    assert _agent_funding_payments(conn, "funding_carry_v1", now - DAY) == []


def test_paper_rows_never_claim_live_funding(conn):
    """funding_payments are REAL account cash flows: a paper agent 'holding'
    the coin in its paper book (is_paper=1 decision rows, no fills) must get
    no share — the equal-split fallback used to leak paper holders in."""
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, coin, is_paper) VALUES(?,?,?,?,1)",
        (now - 600_000, "breakout_v1", "place", "BTC"),
    )
    _funding(conn, "BTC", now - 300_000, 2.50)
    assert _agent_funding_payments(conn, "breakout_v1", now - DAY) == []
    # A live holder over the same span still gets the full payment.
    _decision(conn, "funding_carry_v1", "place", "BTC", now - 600_000)
    pays = _agent_funding_payments(conn, "funding_carry_v1", now - DAY)
    assert sum(s for _, s in pays) == pytest.approx(2.50)


def test_agents_funding_since_rolls_up_and_dedups(conn):
    from hl_bot.scoring.metrics import agents_funding_since

    now = int(time.time() * 1000)
    # A long 3, B long 1 -> $4 split 3:1 by size; manual-only coin excluded.
    _fill(conn, "xfund_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=3.0)
    _fill(conn, "funding_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=1.0)
    _funding(conn, "BTC", now - 300_000, -4.0)
    total = agents_funding_since(
        conn, ["xfund_carry_v1", "funding_carry_v1", "xfund_carry_v1"], now - DAY)
    assert total == pytest.approx(-4.0)  # repeated name does not double-count
    assert agents_funding_since(conn, ["xfund_carry_v1"], now - DAY) == pytest.approx(-3.0)


def test_agents_funding_breakdown_per_agent_values(conn):
    """The breakdown carries each agent's signed share separately (the
    daily-loss guardrail clamps income per agent), dedups repeated names,
    and sums to agents_funding_since."""
    from hl_bot.scoring.metrics import agents_funding_breakdown, agents_funding_since

    now = int(time.time() * 1000)
    _fill(conn, "xfund_carry_v1", "BTC", now - 600_000, pnl=0.0, fee=0.0, sz=3.0)
    _fill(conn, "funding_carry_v1", "ETH", now - 600_000, pnl=0.0, fee=0.0, sz=1.0)
    _funding(conn, "BTC", now - 300_000, -3.0)
    _funding(conn, "ETH", now - 300_000, 2.0)

    bd = agents_funding_breakdown(
        conn, ["xfund_carry_v1", "funding_carry_v1", "xfund_carry_v1"], now - DAY)
    assert set(bd) == {"xfund_carry_v1", "funding_carry_v1"}
    assert bd["xfund_carry_v1"] == pytest.approx(-3.0)
    assert bd["funding_carry_v1"] == pytest.approx(2.0)
    assert agents_funding_since(
        conn, ["xfund_carry_v1", "funding_carry_v1"], now - DAY
    ) == pytest.approx(sum(bd.values()))


def test_funding_not_attributed_outside_holding_window(conn):
    now = int(time.time() * 1000)
    _decision(conn, "funding_carry_v1", "place", "SOL", now - 600_000)
    _decision(conn, "funding_carry_v1", "flatten", "SOL", now - 500_000)
    _funding(conn, "SOL", now - 100_000, 1.0)  # after the position closed
    assert _agent_funding_payments(conn, "funding_carry_v1", now - DAY) == []


def test_per_agent_sharpe_from_daily_pnl(conn):
    now = int(time.time() * 1000)
    for i, pnl in enumerate([8.0, 12.0, 10.0, 9.0, 11.0]):  # 5 distinct days, varying
        _fill(conn, "twap_mr_v1", "BTC", now - i * DAY, pnl=pnl)
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.sharpe is not None and sc.sharpe > 0


def test_single_day_has_no_sharpe(conn):
    now = int(time.time() * 1000)
    for i in range(4):
        _fill(conn, "twap_mr_v1", "BTC", now - i * 1000, pnl=5.0)  # all same day
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.sharpe is None


def test_per_agent_drawdown_needs_capital_base(conn):
    now = int(time.time() * 1000)
    # +100, +100, -300 on 3 distinct (chronological) days.
    for day, pnl in ((2, 100.0), (1, 100.0), (0, -300.0)):
        _fill(conn, "twap_mr_v1", "BTC", now - day * DAY, pnl=pnl, fee=0.0)
    # Without a capital base, drawdown stays N/A (the historical bug).
    assert score_agent(conn, "twap_mr_v1", "all").max_drawdown is None
    # With a base, equity = [1000, 1100, 1200, 900] -> -25% drawdown.
    sc = score_agent(conn, "twap_mr_v1", "all", capital_base=1000.0)
    assert sc.max_drawdown == pytest.approx(-0.25)
    # B-CALMAR: 3 days is far too short to annualize -> suppressed, not absurd.
    assert sc.calmar is None


def test_calmar_needs_30_daily_observations():
    # B-CALMAR: (1+mean)^365 on a days-old series compounds into absurdity
    # (a live record printed +7.9e45). Below MIN_CALMAR_DAYS observations the
    # calmar is None; the drawdown is reported regardless.
    from hl_bot.scoring.metrics import MIN_CALMAR_DAYS, _daily_pnl_drawdown

    short = [50.0] * (MIN_CALMAR_DAYS - 2) + [-100.0]  # 29 days, hot mean + dip
    dd, calmar = _daily_pnl_drawdown(short, 1000.0)
    assert dd is not None and dd < 0
    assert calmar is None

    long = [5.0] * (MIN_CALMAR_DAYS - 1) + [-100.0]  # 30 days
    dd, calmar = _daily_pnl_drawdown(long, 1000.0)
    assert dd is not None and dd < 0
    assert calmar is not None


def test_account_calmar_suppressed_on_short_window(conn):
    # B-CALMAR, account arm: a hot week of equity snapshots must not print an
    # annualized calmar; a 30+-day curve still does.
    def _snap(t_ms, value):
        conn.execute(
            """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
               total_ntl_pos, total_raw_usd, withdrawable, cross_leverage, raw_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (t_ms, value, 0.0, 0.0, value, value, None, "{}"),
        )

    now = int(time.time() * 1000)
    # 6 days: +30%/day with one dip -> hot mean, real drawdown, tiny sample.
    values = [100.0, 130.0, 169.0, 150.0, 195.0, 253.0]
    for i, v in enumerate(values):
        _snap(now - (len(values) - 1 - i) * DAY, v)
    sc = score_agent(conn, "_account", "all")
    assert sc.max_drawdown is not None and sc.max_drawdown < 0
    assert sc.calmar is None

    # Extend the curve past 30 daily returns -> calmar evaluates again.
    for i in range(1, 32):
        _snap(now - len(values) * DAY - i * DAY, 100.0 - i * 0.1)
    sc = score_agent(conn, "_account", "all")
    assert sc.calmar is not None


def test_drawdown_guardrail_can_fire(conn):
    from hl_bot.supervisor.goals import AgentGoals, evaluate

    now = int(time.time() * 1000)
    for day, pnl in ((2, 100.0), (1, 100.0), (0, -300.0)):
        _fill(conn, "twap_mr_v1", "BTC", now - day * DAY, pnl=pnl, fee=0.0)
    g = AgentGoals.model_validate({
        "agent": "twap_mr_v1",
        "capital": 1000,
        "guardrails": [
            {"metric": "max_drawdown", "window": "all", "op": ">=",
             "threshold": -0.10, "action": "demote", "reason": "dd > 10%"},
        ],
    })
    evals = {e.goal_name: e for e in evaluate(conn, g)}
    gr = evals["guardrail:max_drawdown"]
    # -25% breaches the -10% floor -> the guardrail fires (no longer N/A).
    assert gr.status == "fail" and gr.action == "demote"
