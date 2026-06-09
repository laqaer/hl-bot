"""Tests for liq_cascade — fed-or-inert behavior (REVIEW C6 / backlog B11).

liq_cascade can only be fed by the WS ``trades`` liquidation stream (there is no
public REST liquidations endpoint). These tests pin both halves of the contract:
when WS-shaped liquidation events are present it enters in the cascade direction;
when none are present it safely holds (it is effectively disabled, never blind).
"""

from __future__ import annotations

import time

from hl_bot.agents.base import MarketView
from hl_bot.agents.liq_cascade import LiqCascadeAgent
from hl_bot.db.schema import init_db


def _view(liqs: list[dict], mids: dict[str, float], vol: dict[str, float]) -> MarketView:
    return MarketView(
        ts_ms=int(time.time() * 1000),
        mids=mids,
        extra={"liquidations": liqs, "day_ntl_vlm": vol},
    )


def test_holds_when_no_liquidations_fed(tmp_path):
    """No WS feed (empty liquidations) -> the agent holds, never errors."""
    conn = init_db(tmp_path / "lc.sqlite")
    agent = LiqCascadeAgent(conn=conn)
    out = agent.decide(_view([], {"BTC": 64000.0}, {"BTC": 5e8}))
    assert len(out) == 1
    assert out[0].action == "hold"


def test_enters_same_side_as_cascade_when_fed(tmp_path):
    """A >$100k liquidation of shorts (side 'B', forced buys) on a high-volume
    coin -> enter LONG (side 'B') in the direction of the cascade pressure."""
    conn = init_db(tmp_path / "lc.sqlite")
    agent = LiqCascadeAgent(conn=conn)
    now = int(time.time() * 1000)
    # WS-snapshot-shaped event (as produced by MarketState.recent_liquidations).
    liqs = [{
        "coin": "ETH", "side": "B", "px": 3000.0, "sz": 50.0,
        "ts_ms": now, "notional_usd": 150_000.0, "liquidation": True,
    }]
    out = agent.decide(_view(liqs, {"ETH": 3000.0}, {"ETH": 5e8}))
    places = [d for d in out if d.action == "place"]
    assert len(places) == 1
    assert places[0].coin == "ETH"
    assert places[0].side == "B"  # long, same side as the liquidated shorts


def test_ignores_liquidations_on_thin_coins(tmp_path):
    """Below the daily-volume floor -> no entry even with a big liquidation."""
    conn = init_db(tmp_path / "lc.sqlite")
    agent = LiqCascadeAgent(conn=conn)
    now = int(time.time() * 1000)
    liqs = [{
        "coin": "TINY", "side": "A", "px": 1.0, "sz": 200_000.0,
        "ts_ms": now, "notional_usd": 200_000.0, "liquidation": True,
    }]
    out = agent.decide(_view(liqs, {"TINY": 1.0}, {"TINY": 1_000.0}))
    assert all(d.action != "place" for d in out)


def test_ignores_stale_liquidations_outside_window(tmp_path):
    """Events older than the lookback window don't trigger an entry."""
    conn = init_db(tmp_path / "lc.sqlite")
    agent = LiqCascadeAgent(conn=conn)
    old = int(time.time() * 1000) - 10 * 60 * 1000  # 10min ago, window is 5min
    liqs = [{
        "coin": "ETH", "side": "B", "px": 3000.0, "sz": 50.0,
        "ts_ms": old, "notional_usd": 150_000.0, "liquidation": True,
    }]
    out = agent.decide(_view(liqs, {"ETH": 3000.0}, {"ETH": 5e8}))
    assert all(d.action != "place" for d in out)
