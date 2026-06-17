
from hl_bot.cli.factories import agent_config
from hl_bot.db.schema import init_db
from hl_bot.supervisor.goals import AgentGoals, Condition, Promotion
from hl_bot.supervisor.loop import run_once


def _make_goals(agent: str) -> AgentGoals:
    return AgentGoals(
        agent=agent,
        mode="paper",
        promotion=Promotion(
            from_mode="paper",
            to_mode="live_small",
            conditions=[Condition(metric="n_trades", window="30d", op=">=", threshold=0)],
        ),
    )


def test_supervisor_blocks_paper_to_live_small_without_confirmation(tmp_path):
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES(?,?,?)",
        ("twap_mr_v1", "paper", 1),
    )
    # No confirmed_params_hash
    actions = run_once(conn, [_make_goals("twap_mr_v1")])
    assert any("BLOCKED PROMOTE" in a for a in actions.get("twap_mr_v1", []))
    row = conn.execute("SELECT mode FROM agent_state WHERE agent=?", ("twap_mr_v1",)).fetchone()
    assert row["mode"] == "paper"


def test_supervisor_allows_paper_to_live_small_with_matching_confirmation(tmp_path):
    conn = init_db(":memory:")
    _, deployed_hash = agent_config("twap_mr_v1")
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled, confirmed_params_hash) VALUES(?,?,?,?)",
        ("twap_mr_v1", "paper", 1, deployed_hash),
    )
    actions = run_once(conn, [_make_goals("twap_mr_v1")])
    assert any("PROMOTE" in a for a in actions.get("twap_mr_v1", []))
    row = conn.execute("SELECT mode FROM agent_state WHERE agent=?", ("twap_mr_v1",)).fetchone()
    assert row["mode"] == "live_small"
