"""Liq-cascade feed gating (REVIEW C6 / B11).

The agent may only open new positions when a *real* liquidation feed is present
(the WS `trades` liquidation flag, or a backtest's synthetic feed). Without it,
the legacy REST endpoint was a phantom that always returned nothing, so a "no
liquidations" reading is ambiguous — entries stay disabled. Exits must keep
running even when the feed drops, so a live position is never stranded.
"""

from __future__ import annotations

import time

from hl_bot.agents.base import MarketView
from hl_bot.agents.liq_cascade import LiqCascadeAgent
from hl_bot.db.schema import init_db


def _view(liqs, *, feed, vol=None):
    now_ms = int(time.time() * 1000)
    return MarketView(
        ts_ms=now_ms,
        mids={"BTC": 100.0},
        extra={
            "liquidations": liqs,
            "liquidations_feed": feed,
            "day_ntl_vlm": vol or {"BTC": 1_000_000_000.0},
        },
    )


def _big_liq():
    return [{
        "coin": "BTC",
        "side": "B",  # shorts liquidated -> forced buys -> long
        "notional_usd": 250_000.0,
        "ts_ms": int(time.time() * 1000),
    }]


def test_no_feed_suppresses_entries():
    agent = LiqCascadeAgent(conn=None)
    # Even with a fat liquidation present, no real feed => no entry.
    out = agent.decide(_view(_big_liq(), feed=False))
    assert [d.action for d in out] == ["hold"]
    assert out[0].market_snapshot["liquidations_feed"] is False


def test_real_feed_enters_same_side_as_cascade():
    agent = LiqCascadeAgent(conn=None)
    out = agent.decide(_view(_big_liq(), feed=True))
    places = [d for d in out if d.action == "place"]
    assert len(places) == 1
    # shorts liquidated (side 'B') => trade long (side 'B')
    assert places[0].coin == "BTC"
    assert places[0].side == "B"


def test_real_feed_but_calm_market_holds():
    agent = LiqCascadeAgent(conn=None)
    out = agent.decide(_view([], feed=True))
    assert [d.action for d in out] == ["hold"]
    # distinct from the no-feed hold: this one reports it saw 0 events
    assert out[0].market_snapshot.get("n_events") == 0


def test_exits_run_even_without_feed(tmp_path):
    conn = init_db(tmp_path / "s.sqlite")
    # Open a long at 100 that is now deep in the money (mid 110 -> TP).
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px) "
        "VALUES(?, 'liq_cascade_v1', 'place', 'BTC', 'B', 1.0, 100.0)",
        (int(time.time() * 1000) - 60_000,),
    )
    agent = LiqCascadeAgent(conn=conn)
    view = _view([], feed=False)
    view.mids["BTC"] = 110.0
    out = agent.decide(view)
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1
    assert flats[0].coin == "BTC"
    # no entry leaked through on a dead feed
    assert all(d.action != "place" for d in out)
