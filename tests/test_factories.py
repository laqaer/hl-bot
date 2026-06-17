from hl_bot.cli.factories import (
    agent_config,
    agent_factory,
    list_confirmable_agents,
    make_agent_factory,
)
from hl_bot.config_hash import hash_config
from hl_bot.db.schema import init_db


def test_agent_config_merges_overrides():
    overrides = {"femr_v1": {"max_notional_per_trade": 99.0}}
    cfg, h = agent_config("femr_v1", overrides=overrides)
    assert cfg["max_notional_per_trade"] == 99.0
    assert cfg["funding_enter_per_hr"] == 0.00015
    assert h == hash_config(cfg)


def test_agent_factory_persists_config():
    conn = init_db(":memory:")
    agent = agent_factory("twap_mr_v1", conn=conn)
    row = conn.execute(
        "SELECT params_hash FROM agent_configs WHERE agent=?", (agent.name,)
    ).fetchone()
    assert row is not None
    assert row["params_hash"] == agent.params_hash


def test_factory_lambda_works_like_backtest_factory():
    conn = init_db(":memory:")
    factory = make_agent_factory("femr_v1")
    agent = factory(conn)
    assert agent.name == "femr_v1"
    assert agent.params_hash


def test_confirmable_agents_include_existing_strategies():
    agents = list_confirmable_agents()
    assert "femr_v1" in agents
    assert "twap_mr_regime_v1" in agents
