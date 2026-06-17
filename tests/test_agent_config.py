
from hl_bot.agents.femr import FemrAgent
from hl_bot.config_hash import hash_config
from hl_bot.db.schema import init_db


def test_agent_computes_params_hash():
    cfg = {"max_notional_per_trade": 42.0}
    agent = FemrAgent(config=cfg)
    assert agent.params_hash == hash_config(cfg)


def test_agent_persists_config_on_instantiation():
    conn = init_db(":memory:")
    cfg = {"max_notional_per_trade": 42.0}
    agent = FemrAgent(config=cfg, conn=conn)
    row = conn.execute(
        "SELECT agent, params_hash, config_json FROM agent_configs WHERE agent=?",
        (agent.name,),
    ).fetchone()
    assert row is not None
    assert row["agent"] == "femr_v1"
    assert row["params_hash"] == agent.params_hash
    assert "max_notional_per_trade" in row["config_json"]


def test_agent_without_conn_does_not_persist():
    agent = FemrAgent(config={"max_notional_per_trade": 1.0})
    assert agent.params_hash
    # No exception; no DB side effects to check.


def test_decision_logs_params_hash():
    from hl_bot.agents.decisions import Decision, log_decision

    conn = init_db(":memory:")
    d = Decision(
        agent="test_v1",
        action="hold",
        params_hash="deadbeef12345678",
    )
    log_decision(conn, d)
    row = conn.execute(
        "SELECT params_hash FROM agent_decisions WHERE agent=?", (d.agent,)
    ).fetchone()
    assert row["params_hash"] == "deadbeef12345678"
