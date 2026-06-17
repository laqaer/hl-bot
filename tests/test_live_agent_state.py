"""Live-mode agent-state gating.

Live cron may be running, but only agents explicitly enabled and promoted to
live_small/live should be allowed into the live execution roster. Paper or
paused agents must keep evaluating in paper/research paths without being able to
place live orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from hl_bot.db.schema import init_db
from hl_bot.engine.runner import RosterEntry, split_roster
from hl_bot.supervisor.goals import AgentGoals


@dataclass
class DummyAgent:
    name: str
    is_live: bool = False


def _goals(name, roster="live"):
    return AgentGoals.model_validate({"agent": name, "roster": roster})


def _entry(name, roster="live"):
    return RosterEntry(agent=DummyAgent(name), goals=_goals(name, roster))


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

    roster = [_entry("femr_v1"), _entry("twap_mr_v1"), _entry("basis_v1")]
    live, paper = split_roster(conn, roster, live=True)

    assert [e.agent.name for e in live] == ["femr_v1"]
    assert {e.agent.name for e in paper} == {"twap_mr_v1"}
    assert "basis_v1" not in {e.agent.name for e in live + paper}


def test_missing_agent_state_is_paper_by_default(tmp_path):
    conn = init_db(tmp_path / "state.sqlite")

    live, paper = split_roster(conn, [_entry("liq_cascade_v1")], live=True)

    assert live == []
    assert [e.agent.name for e in paper] == ["liq_cascade_v1"]
