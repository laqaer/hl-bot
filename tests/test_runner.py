"""Consolidated engine cycle: roster splitting, paper agents never reach the
live executor, kill blocks entries but not flattens, maker entries rest."""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import Agent, MarketView
from hl_bot.agents.decisions import Decision
from hl_bot.config import Settings
from hl_bot.db.schema import init_db
from hl_bot.engine.runner import RosterEntry, run_cycle, split_roster
from hl_bot.ops.kill import trip_kill
from hl_bot.supervisor.goals import AgentGoals

NOW = int(time.time() * 1000)


class ScriptedAgent(Agent):
    """Emits a fixed list of decisions every cycle."""

    def __init__(self, name, decisions):
        super().__init__(name, {})
        self._decisions = decisions

    def decide(self, view):
        import copy
        return copy.deepcopy(self._decisions)


class FakeExchange:
    def __init__(self):
        self.market_opens: list[str] = []
        self.limit_orders: list[str] = []
        self.closed: list[str] = []

    def market_open(self, *, name, is_buy, sz, slippage, cloid, builder=None):
        self.market_opens.append(name)
        return {"response": {"data": {"statuses": [
            {"filled": {"avgPx": "100.0", "totalSz": str(sz), "oid": 5}}]}}}

    def order(self, *, name, is_buy, sz, limit_px, order_type, reduce_only, cloid, builder=None):
        self.limit_orders.append(name)
        return {"response": {"data": {"statuses": [
            {"resting": {"oid": 6, "cloid": str(cloid)}}]}}}

    def market_close(self, coin, cloid=None):
        self.closed.append(coin)
        return {"response": {"data": {"statuses": [
            {"filled": {"avgPx": "100.0", "totalSz": "1.0", "oid": 7}}]}}}

    class info:  # noqa: N801
        @staticmethod
        def meta():
            return {"universe": [{"name": "BTC", "szDecimals": 3}]}


def goals(name, roster="live", cooldown_s=0):
    return AgentGoals.model_validate({"agent": name, "roster": roster,
                                      "cooldown_s": cooldown_s})


def entry(name, decisions, roster="live"):
    return RosterEntry(agent=ScriptedAgent(name, decisions), goals=goals(name))


def place(agent, coin="BTC", sz=1.0):
    from hl_bot.agents.cloid import make_cloid
    return Decision(agent=agent, action="place", coin=coin, side="B", sz=sz,
                    px=100.0, cloid=make_cloid(agent))


def flatten(agent, coin="BTC"):
    from hl_bot.agents.cloid import make_cloid
    return Decision(agent=agent, action="flatten", coin=coin, cloid=make_cloid(agent))


@pytest.fixture()
def env(tmp_path):
    conn = init_db(tmp_path / "data" / "t.sqlite")
    s = Settings(hl_address="0x0", hl_secret_key=None,
                 hl_api_url="http://unused", tg_bot_token=None, tg_chat_id=None,
                 db_path=tmp_path / "data" / "t.sqlite", paper_mode_default=True)
    return conn, s


def set_mode(conn, agent, mode, enabled=1):
    conn.execute(
        "INSERT OR REPLACE INTO agent_state(agent, mode, enabled) VALUES(?,?,?)",
        (agent, mode, enabled),
    )


def view():
    return MarketView(ts_ms=NOW, mids={"BTC": 100.0}, funding={"BTC": 0.0001})


def test_split_roster_modes(env):
    conn, _ = env
    roster = [entry("a_live", []), entry("a_paper", []), entry("a_paused", [])]
    set_mode(conn, "a_live", "live_small")
    set_mode(conn, "a_paused", "live", enabled=0)
    live, paper = split_roster(conn, roster, live=True)
    assert [e.agent.name for e in live] == ["a_live"]
    assert [e.agent.name for e in paper] == ["a_paper"]   # paused runs nowhere

    live, paper = split_roster(conn, roster, live=False)
    assert live == []
    assert len(paper) == 2


def test_paper_agents_never_reach_executor(env):
    conn, s = env
    set_mode(conn, "a_live", "live_small")
    roster = [entry("a_live", [place("a_live")]),
              entry("a_paper", [place("a_paper", coin="BTC")])]
    ex = FakeExchange()
    run_cycle(conn, s, view(), live=True, execution="taker",
              roster=roster, exchange=ex, account_state={}, spot_state={})
    assert ex.market_opens == ["BTC"]          # only the live agent traded
    # the paper agent's trade went to the simulator
    rows = conn.execute("SELECT agent FROM paper_fills").fetchall()
    assert {r["agent"] for r in rows} == {"a_paper"}


def test_kill_blocks_entries_not_flattens(env):
    conn, s = env
    trip_kill(s.db_path.parent, "test", alert=False)
    set_mode(conn, "a_live", "live")
    roster = [entry("a_live", [place("a_live"), flatten("a_live")])]
    ex = FakeExchange()
    res = run_cycle(conn, s, view(), live=True, execution="taker",
                    roster=roster, exchange=ex, account_state={}, spot_state={})
    assert ex.market_opens == []               # entry blocked
    assert ex.closed == ["BTC"]                # risk reduction allowed
    assert any("KILL" in e for e in res.events)


def test_maker_entries_rest_and_dedupe(env):
    conn, s = env
    set_mode(conn, "a_live", "live_small")
    roster = [entry("a_live", [place("a_live")])]
    ex = FakeExchange()
    res1 = run_cycle(conn, s, view(), live=True, execution="maker",
                     roster=roster, exchange=ex, account_state={}, spot_state={})
    assert ex.limit_orders == ["BTC"]
    assert any("RESTING" in e for e in res1.events)
    # Second cycle with a quote already resting on the same coin: no stack-up.
    res2 = run_cycle(conn, s, view(), live=True, execution="maker",
                     roster=roster, exchange=ex, account_state={}, spot_state={})
    assert ex.limit_orders == ["BTC"]          # still just one
    assert any("already resting" in e for e in res2.events)


def test_cooldown_respected(env):
    conn, s = env
    set_mode(conn, "a_live", "live_small")
    g = goals("a_live", cooldown_s=3600)
    roster = [RosterEntry(agent=ScriptedAgent("a_live", [place("a_live")]), goals=g)]
    ex = FakeExchange()
    run_cycle(conn, s, view(), live=True, execution="taker",
              roster=roster, exchange=ex, account_state={}, spot_state={})
    res = run_cycle(conn, s, view(), live=True, execution="taker",
                    roster=roster, exchange=ex, account_state={}, spot_state={})
    assert ex.market_opens == ["BTC"]          # second attempt suppressed
    assert any("cooldown" in e for e in res.events)


def test_paper_mode_simulates_everything(env):
    conn, s = env
    roster = [entry("a1", [place("a1")]), entry("a2", [place("a2")])]
    res = run_cycle(conn, s, view(), live=False, roster=roster, execution="taker")
    assert res.live_agents == []
    rows = conn.execute("SELECT DISTINCT agent FROM paper_fills").fetchall()
    assert {r["agent"] for r in rows} == {"a1", "a2"}
