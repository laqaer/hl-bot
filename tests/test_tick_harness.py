"""Tests for the shared live/paper tick-harness pieces in ``runtime``.

These functions were extracted from the inlined, untested ``femr_tick`` preamble
(REVIEW M3 / B12) so the live path shares tested code with the paper path:

- ``positions_from_clearinghouse`` — pure parse of HL ``clearinghouseState`` into
  the bot's position-dict shape, skipping malformed entries.
- ``reconcile_agents`` — per-agent stale-ownership reconcile against HL truth,
  returning only the agents that had something cleared.
- ``apply_allocator_caps`` — allocate the 7d split, resolve the layered risk
  rule, and write the binding caps onto each agent's cfg.
- ``overlay_ws_snapshot`` — additive merge of a fresh WS snapshot onto the live
  REST view, enabling the real liquidations feed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.runtime import (
    apply_allocator_caps,
    overlay_ws_snapshot,
    positions_from_clearinghouse,
    reconcile_agents,
)
from hl_bot.db.schema import init_db
from hl_bot.risk.scaling import NotionalCap


def _risk_cap(max_total: float, max_per_pos: float) -> NotionalCap:
    return NotionalCap(
        max_total_notional=max_total,
        max_per_position_notional=max_per_pos,
        portfolio_value=max_per_pos,
        avg_account_value=max_per_pos,
        multiplier=5.0,
        per_position_multiplier=1.0,
        ceiling_notional=None,
        lookback_days=7,
        sample_count=1,
        source="test",
    )


def _agent(name: str, cfg=None):
    return SimpleNamespace(name=name, cfg=cfg) if cfg is not None else SimpleNamespace(name=name)


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


def test_apply_allocator_caps_honors_explicit_cfg_and_mutates(conn):
    # Empty fills -> cold-start: allocator floors each agent at min_alloc (50).
    # An explicit sub-legacy total (30) is honored; per-trade (10) is preserved.
    cfg = SimpleNamespace(max_total_notional=30.0, max_notional_per_trade=10.0)
    agents = [_agent("a", cfg)]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=500.0, max_per_pos=100.0))
    # min(alloc=50, approved_total=30, per_pos=100) = 30.
    assert out.effective_caps["a"] == 30.0
    assert out.effective_order_caps["a"] == 10.0
    # The agent's cfg is mutated in place with the binding caps.
    assert cfg.max_total_notional == 30.0
    assert cfg.max_notional_per_trade == 10.0


def test_apply_allocator_caps_caps_at_per_position_ceiling(conn):
    # No configured cap -> dynamic 1x per-position ceiling binds. alloc floors at
    # 50, per_pos ceiling is 40, so total = min(50, 40) = 40.
    cfg = SimpleNamespace(max_total_notional=float("inf"), max_notional_per_trade=float("inf"))
    agents = [_agent("a", cfg)]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=200.0, max_per_pos=40.0))
    assert out.effective_caps["a"] == 40.0
    assert cfg.max_total_notional == 40.0


def test_apply_allocator_caps_agent_without_cfg_left_untouched(conn):
    # An agent with no cfg keeps its raw alloc in effective_caps and is not
    # mutated (no cfg to write).
    agents = [_agent("a")]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=500.0, max_per_pos=100.0))
    assert out.effective_caps["a"] == out.allocs["a"]
    assert "a" not in out.effective_order_caps
    assert not hasattr(agents[0], "cfg")


def test_overlay_ws_snapshot_none_is_noop():
    view = MarketView(ts_ms=0, mids={"BTC": 100.0}, funding={"BTC": 0.0001})
    out = overlay_ws_snapshot(view, None)
    assert out.applied is False
    assert out.n_mids == 0 and out.n_liqs == 0
    # REST view untouched; no liquidations feed flag injected.
    assert view.mids == {"BTC": 100.0}
    assert "liquidations_feed" not in view.extra


def test_overlay_ws_snapshot_merges_and_enables_feed():
    view = MarketView(
        ts_ms=0,
        mids={"BTC": 100.0, "ETH": 50.0},
        funding={"BTC": 0.0001},
        book_top={"BTC": (99.0, 101.0)},
    )
    snap = MarketView(
        ts_ms=1,
        mids={"BTC": 100.5},  # fresher BTC mid overrides; ETH preserved
        funding={"BTC": 0.0002, "SOL": 0.0003},
        book_top={"SOL": (9.9, 10.1)},
        extra={"liquidations": [{"coin": "BTC"}, {"coin": "ETH"}]},
    )
    out = overlay_ws_snapshot(view, snap)
    assert out.applied is True
    assert out.n_mids == 1 and out.n_liqs == 2
    assert view.mids == {"BTC": 100.5, "ETH": 50.0}
    assert view.funding == {"BTC": 0.0002, "SOL": 0.0003}
    assert view.book_top == {"BTC": (99.0, 101.0), "SOL": (9.9, 10.1)}
    assert view.extra["liquidations"] == [{"coin": "BTC"}, {"coin": "ETH"}]
    assert view.extra["liquidations_feed"] is True


def test_overlay_ws_snapshot_empty_liqs_still_enables_feed():
    # A fresh snapshot with no liquidations is a calm market, NOT a broken feed:
    # the feed flag is still set so liq_cascade entries are enabled.
    view = MarketView(ts_ms=0, mids={"BTC": 100.0})
    snap = MarketView(ts_ms=1, mids={"BTC": 100.0}, extra={})
    out = overlay_ws_snapshot(view, snap)
    assert out.applied is True
    assert out.n_liqs == 0
    assert view.extra["liquidations"] == []
    assert view.extra["liquidations_feed"] is True
