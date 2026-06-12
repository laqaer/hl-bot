"""Live-mode agent-state gating.

Live cron may be running, but only agents explicitly enabled and promoted to
live_small/live should be allowed into the live execution roster. Paper or
paused agents must keep evaluating in paper/research paths without being able to
place live orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.agents.runtime import filter_live_agents
from hl_bot.db.schema import init_db


@dataclass
class DummyAgent:
    name: str


def test_live_roster_requires_enabled_live_mode(tmp_path):
    conn = init_db(tmp_path / "state.sqlite")
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('femr_v1', 'live_small', 1)"
    )
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('twap_mr_v1', 'paper', 1)"
    )
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('basis_v1', 'live_small', 0)"
    )

    agents = [DummyAgent("femr_v1"), DummyAgent("twap_mr_v1"), DummyAgent("basis_v1")]
    live_agents, skipped = filter_live_agents(conn, agents)

    assert [a.name for a in live_agents] == ["femr_v1"]
    assert skipped == {
        "twap_mr_v1": "mode=paper enabled=1",
        "basis_v1": "mode=live_small enabled=0",
    }


def test_missing_agent_state_is_paper_by_default(tmp_path):
    conn = init_db(tmp_path / "state.sqlite")

    live_agents, skipped = filter_live_agents(conn, [DummyAgent("liq_cascade_v1")])

    assert live_agents == []
    assert skipped == {"liq_cascade_v1": "mode=paper enabled=1"}
