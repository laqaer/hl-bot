"""Tests for the shared decision-gathering path (``runtime.gather_decisions``).

This is the single function both the paper ``tick`` loop (``run_tick``) and the
live ``femr_tick`` loop use to ask agents to ``decide()`` and log results (REVIEW
M3 — the two paths had diverged). The safety-critical behavior to pin down:

- a ``decide()`` that raises is isolated as an ``error`` row, so one broken agent
  cannot abort the whole tick (previously ``femr_tick`` had no isolation);
- the live policy (``defer_exec_logging`` + ``log_holds=False``) returns
  place/flatten/hold rows but does NOT log them, so the cooldown check never sees
  our own intent rows;
- the paper policy logs everything immediately;
- ``honor_enabled`` skips agents disabled in ``agent_state``.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import Agent, MarketView
from hl_bot.agents.decisions import Decision
from hl_bot.agents.runtime import gather_decisions
from hl_bot.db.schema import init_db


class _FakeAgent(Agent):
    def __init__(self, name: str, decisions: list[Decision]):
        super().__init__(name=name)
        self._decisions = decisions

    def decide(self, view: MarketView) -> list[Decision]:
        return self._decisions


class _BoomAgent(Agent):
    def decide(self, view: MarketView) -> list[Decision]:
        raise RuntimeError("kaboom")


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _view():
    return MarketView(ts_ms=0, mids={"BTC": 100.0})


def _logged(conn, agent, action):
    return conn.execute(
        "SELECT action, is_paper FROM agent_decisions WHERE agent=? AND action=?",
        (agent, action),
    ).fetchall()


def test_paper_policy_logs_everything_immediately(conn):
    agent = _FakeAgent("a", [
        Decision(agent="a", action="hold", coin="BTC"),
        Decision(agent="a", action="place", coin="BTC", side="B", sz=0.01, px=100.0),
    ])
    out = gather_decisions(conn, [agent], _view(), is_paper=True)
    assert [d.action for d in out] == ["hold", "place"]
    assert all(d.is_paper for d in out)
    assert _logged(conn, "a", "hold")    # holds logged under the paper policy
    assert _logged(conn, "a", "place")   # place logged immediately too


def test_live_policy_defers_place_flatten_and_skips_holds(conn):
    agent = _FakeAgent("a", [
        Decision(agent="a", action="hold", coin="BTC"),
        Decision(agent="a", action="place", coin="BTC", side="B", sz=0.01, px=100.0),
        Decision(agent="a", action="flatten", coin="ETH"),
        Decision(agent="a", action="cancel", coin="SOL"),
    ])
    out = gather_decisions(
        conn, [agent], _view(),
        is_paper=False, defer_exec_logging=True, log_holds=False, honor_enabled=False,
    )
    # All four still returned for the executor / display…
    assert [d.action for d in out] == ["hold", "place", "flatten", "cancel"]
    assert all(d.is_paper is False for d in out)
    # …but only the non-exec, non-hold action is logged now.
    assert not _logged(conn, "a", "hold")
    assert not _logged(conn, "a", "place")
    assert not _logged(conn, "a", "flatten")
    assert _logged(conn, "a", "cancel")


def test_crashing_agent_is_isolated_so_others_still_run(conn):
    boom = _BoomAgent(name="boom")
    healthy = _FakeAgent("safe", [Decision(agent="safe", action="flatten", coin="BTC")])
    out = gather_decisions(
        conn, [boom, healthy], _view(),
        is_paper=False, defer_exec_logging=True, log_holds=False, honor_enabled=False,
    )
    # The healthy agent's risk-reducing flatten survives the crash.
    assert [d.agent for d in out] == ["safe"]
    # The crash is recorded as an error row (always paper), not swallowed silently.
    err = _logged(conn, "boom", "error")
    assert len(err) == 1 and err[0]["is_paper"] == 1


def test_honor_enabled_skips_disabled_agents(conn):
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('off','paper',0)")
    off = _FakeAgent("off", [Decision(agent="off", action="hold", coin="BTC")])
    on = _FakeAgent("on", [Decision(agent="on", action="hold", coin="BTC")])
    out = gather_decisions(conn, [off, on], _view(), is_paper=True, honor_enabled=True)
    assert [d.agent for d in out] == ["on"]

    # With honor_enabled=False (the femr path), the disabled row is ignored.
    out2 = gather_decisions(conn, [off, on], _view(), is_paper=True, honor_enabled=False)
    assert sorted(d.agent for d in out2) == ["off", "on"]
