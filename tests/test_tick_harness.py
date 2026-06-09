"""Tests for the shared live/paper tick-harness pieces in ``runtime``.

These functions were extracted from the inlined, untested ``femr_tick`` preamble
(REVIEW M3 / B12) so the live path shares tested code with the paper path:

- ``positions_from_clearinghouse`` — pure parse of HL ``clearinghouseState`` into
  the bot's position-dict shape, skipping malformed entries.
- ``reconcile_agents`` — per-agent stale-ownership reconcile against HL truth,
  returning only the agents that had something cleared.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.runtime import positions_from_clearinghouse, reconcile_agents
from hl_bot.db.schema import init_db


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def test_positions_from_clearinghouse_parses_fields():
    st = {
        "assetPositions": [
            {"position": {
                "coin": "BTC", "szi": "0.5", "entryPx": "100", "positionValue": "50",
                "unrealizedPnl": "1.5", "liquidationPx": "80",
                "leverage": {"value": 3}, "marginUsed": "10",
            }},
        ]
    }
    out = positions_from_clearinghouse(st)
    assert len(out) == 1
    p = out[0]
    assert p["coin"] == "BTC"
    assert p["szi"] == 0.5
    assert p["entry_px"] == 100.0
    assert p["leverage"] == 3
    assert p["margin_used"] == 10.0


def test_positions_from_clearinghouse_handles_empty_and_missing():
    assert positions_from_clearinghouse({}) == []
    assert positions_from_clearinghouse({"assetPositions": None}) == []
    # Missing leverage dict and numeric fields default rather than crash.
    out = positions_from_clearinghouse({"assetPositions": [{"position": {"coin": "ETH"}}]})
    assert out == [{
        "coin": "ETH", "szi": 0.0, "entry_px": 0.0, "position_value": 0.0,
        "unrealized_pnl": 0.0, "liquidation_px": 0.0, "leverage": None,
        "margin_used": 0.0,
    }]


def test_reconcile_agents_clears_stale_per_agent(conn):
    # Two agents each "own" a coin per their decision log…
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    log_decision(conn, Decision(agent="b", action="place", coin="ETH", is_paper=False))
    # …but HL truth shows only BTC is live. ETH (agent b) is stale.
    live = [{"coin": "BTC", "szi": 0.5}]
    out = reconcile_agents(conn, live, ["a", "b"])
    assert out == {"b": ["ETH"]}
    # The reconcile wrote a synthetic flatten so b no longer owns ETH.
    from hl_bot.exec.orders import bot_owned_coins
    assert bot_owned_coins(conn, "b") == set()
    assert bot_owned_coins(conn, "a") == {"BTC"}


def test_reconcile_agents_noop_when_all_present(conn):
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    live = [{"coin": "BTC", "szi": 0.5}]
    assert reconcile_agents(conn, live, ["a"]) == {}
