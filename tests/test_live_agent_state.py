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


def test_exit_only_agents_are_those_with_live_inventory(tmp_path):
    """A demoted agent with open live positions or working maker quotes must
    re-enter the live tick exit-only; paper-book state and flat books must not
    (found live, Iter 80: demote-with-open-inventory orphaned a real book)."""
    from hl_bot.agents.cloid import make_cloid
    from hl_bot.agents.decisions import Decision, log_decision
    from hl_bot.agents.runtime import exit_only_live_agents
    from hl_bot.exec.maker import log_rest

    conn = init_db(tmp_path / "state.sqlite")
    # holder: a confirmed live entry, never flattened — owns the coin
    log_decision(conn, Decision(agent="holder_v1", action="place", coin="TON",
                                side="A", sz=10.0, px=1.77, reasoning="entry",
                                is_paper=False))
    # rester: a still-working maker quote (no fill/cancel row yet)
    log_rest(conn, "rester_v1", "NEAR", "A", 5.0, 2.11,
             make_cloid("rester_v1"), oid=7)
    # paper_only: paper-book state must NOT drag an agent into live exit duty
    log_decision(conn, Decision(agent="paper_only_v1", action="place", coin="BTC",
                                side="B", sz=1.0, px=100.0, reasoning="entry",
                                is_paper=True))
    # flat: live round trip already closed — nothing left to manage
    log_decision(conn, Decision(agent="flat_v1", action="place", coin="ETH",
                                side="B", sz=1.0, px=100.0, reasoning="entry",
                                is_paper=False))
    log_decision(conn, Decision(agent="flat_v1", action="flatten", coin="ETH",
                                reasoning="exit", is_paper=False))
    conn.commit()

    agents = [DummyAgent(n) for n in
              ("holder_v1", "rester_v1", "paper_only_v1", "flat_v1", "live_v1")]
    # live_v1 is already in the live roster — never exit-only
    out = exit_only_live_agents(conn, agents, {"live_v1"})
    assert [a.name for a in out] == ["holder_v1", "rester_v1"]
