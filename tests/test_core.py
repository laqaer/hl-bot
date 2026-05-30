import time

import pytest

from hl_bot.agents.cloid import agent_from_cloid, agent_prefix, make_cloid
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.db.schema import init_db
from hl_bot.scoring.metrics import score_agent
from hl_bot.supervisor.goals import AgentGoals, evaluate


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "test.sqlite")


def test_cloid_roundtrip():
    cloid = make_cloid("funding_arb_v1")
    assert cloid.startswith("0xa9e1" + agent_prefix("funding_arb_v1"))
    assert agent_from_cloid(cloid, ["funding_arb_v1"]) == "funding_arb_v1"
    assert agent_from_cloid("0xdeadbeef" + "0" * 24) == "manual"
    assert agent_from_cloid(None) == "manual"
    assert agent_from_cloid(make_cloid("unknown_agent")).startswith("unknown:")


def test_log_decision(conn):
    rowid = log_decision(conn, Decision(
        agent="x", action="hold", reasoning="vibes",
    ))
    assert rowid > 0
    row = conn.execute("SELECT agent, action FROM agent_decisions WHERE id=?", (rowid,)).fetchone()
    assert row["agent"] == "x" and row["action"] == "hold"


def _insert_fill(conn, agent, t_ms, pnl, fee=0.1, sz=1.0, px=100.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{t_ms}", t_ms, t_ms, "BTC", "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_scoring_basic(conn):
    now = int(time.time() * 1000)
    for i, pnl in enumerate([10.0, -5.0, 20.0, -2.0, 15.0]):
        _insert_fill(conn, "alpha", now - 1000 * (i + 1), pnl)
    sc = score_agent(conn, "alpha", "all")
    assert sc.n_trades == 5
    assert sc.realized_pnl == pytest.approx(38.0)
    assert sc.fees_paid == pytest.approx(0.5)
    assert sc.net_pnl == pytest.approx(37.5)
    assert sc.win_rate == pytest.approx(3 / 5)
    assert sc.profit_factor == pytest.approx(45 / 7)
    assert sc.edge_bps is not None and sc.edge_bps > 0


def test_goals_guardrail_pause(conn):
    now = int(time.time() * 1000)
    # losing day: -$300 within 24h
    for i, pnl in enumerate([-100.0, -100.0, -100.0]):
        _insert_fill(conn, "alpha", now - 1000 * (i + 1), pnl)
    g = AgentGoals.model_validate({
        "agent": "alpha",
        "mode": "paper",
        "guardrails": [
            {"metric": "net_pnl", "window": "24h", "op": ">=",
             "threshold": -200, "action": "pause", "reason": "24h loss"},
        ],
    })
    evals = evaluate(conn, g)
    guardrail = next(e for e in evals if e.goal_name.startswith("guardrail"))
    assert guardrail.status == "fail"
    assert guardrail.action == "pause"


def test_goals_primary_pass(conn):
    now = int(time.time() * 1000)
    for i in range(10):
        _insert_fill(conn, "alpha", now - 1000 * (i + 1), 5.0)
    g = AgentGoals.model_validate({
        "agent": "alpha",
        "mode": "paper",
        "goals": {"primary": {"metric": "net_pnl", "window": "all",
                              "op": ">=", "threshold": 10}},
    })
    evals = evaluate(conn, g)
    primary = next(e for e in evals if e.goal_name == "primary")
    assert primary.status == "pass"


def test_guardrail_na_does_not_trigger(conn):
    """Regression: empty agent with no trades had max_drawdown=None which
    was being treated as a guardrail failure and triggering demotion."""
    g = AgentGoals.model_validate({
        "agent": "empty_agent",
        "mode": "paper",
        "guardrails": [
            {"metric": "max_drawdown", "window": "7d", "op": ">=",
             "threshold": -0.10, "action": "demote", "reason": "7d dd > 10%"},
            {"metric": "net_pnl", "window": "24h", "op": ">=",
             "threshold": -200, "action": "pause", "reason": "24h loss"},
        ],
    })
    evals = evaluate(conn, g)
    # max_drawdown has no data -> na, no action
    dd_eval = next(e for e in evals if "max_drawdown" in e.goal_name)
    assert dd_eval.status == "na"
    assert dd_eval.action == "none"
    # net_pnl with no trades = 0, which is >= -200 -> pass
    pnl_eval = next(e for e in evals if "net_pnl" in e.goal_name)
    assert pnl_eval.status == "pass"
    assert pnl_eval.action == "none"
