"""Shared decision-gathering core (REVIEW M3 / B12).

`collect_decisions` is the single loop both the paper tick and the live
`femr_tick` use, so the two paths can't drift. These pin its contract:
a raising agent is isolated (logged as error, others still run), deferred
actions are NOT logged immediately, and is_paper is applied uniformly.
"""

from __future__ import annotations

from hl_bot.agents.base import Agent, MarketView
from hl_bot.agents.decisions import Decision
from hl_bot.agents.runtime import collect_decisions
from hl_bot.db.schema import init_db


class _Boom(Agent):
    def __init__(self) -> None:
        super().__init__("boom")

    def decide(self, view: MarketView) -> list[Decision]:
        raise RuntimeError("kaboom")


class _Scripted(Agent):
    def __init__(self, name: str, decisions: list[Decision]) -> None:
        super().__init__(name)
        self._decisions = decisions

    def decide(self, view: MarketView) -> list[Decision]:
        return self._decisions


def _view() -> MarketView:
    return MarketView(ts_ms=0, mids={"BTC": 100.0})


def _logged(conn):
    return conn.execute(
        "SELECT agent, action, is_paper FROM agent_decisions ORDER BY id"
    ).fetchall()


def test_raising_agent_is_isolated_and_others_still_run(tmp_path):
    conn = init_db(tmp_path / "s.sqlite")
    good = _Scripted("good", [Decision(agent="good", action="cancel", coin="BTC")])

    out = collect_decisions(conn, [_Boom(), good], _view(), is_paper=True)

    # both agents represented in the returned/logged set, tick did not crash
    actions = {(r["agent"], r["action"]) for r in _logged(conn)}
    assert ("boom", "error") in actions
    assert ("good", "cancel") in actions
    # the error decision is logged but NOT returned (only the agent's own outputs)
    assert [d.agent for d in out] == ["good"]


def test_deferred_actions_are_collected_but_not_logged(tmp_path):
    conn = init_db(tmp_path / "s.sqlite")
    a = _Scripted("a", [
        Decision(agent="a", action="place", coin="BTC", side="B", sz=1.0),
        Decision(agent="a", action="hold", coin="ETH"),
        Decision(agent="a", action="cancel", coin="SOL"),
    ])

    out = collect_decisions(
        conn, [a], _view(),
        is_paper=False,
        defer_actions=frozenset({"hold", "place", "flatten"}),
    )

    # all three returned for the caller to act on / display
    assert {d.action for d in out} == {"place", "hold", "cancel"}
    # only the non-deferred action hit the log this tick
    logged = [(r["action"], r["is_paper"]) for r in _logged(conn)]
    assert logged == [("cancel", 0)]


def test_is_paper_flag_applied_uniformly(tmp_path):
    conn = init_db(tmp_path / "s.sqlite")
    a = _Scripted("a", [Decision(agent="a", action="cancel", coin="BTC")])

    out = collect_decisions(conn, [a], _view(), is_paper=True)

    assert out[0].is_paper is True
    assert _logged(conn)[0]["is_paper"] == 1
