"""Tests for the TWAP-MR per-coin loss veto.

The TWAP mean-reversion agent was repeatedly re-entering the same coins (ADA,
HYPE, AVAX...) that had a clearly negative recent edge, bleeding money. The veto
suppresses NEW entries on any coin where this agent's own recent fills show a
material loss AND a bad realized edge, while leaving other coins tradeable.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.db.schema import init_db


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "twap.sqlite")


def _insert_fill(conn, agent, coin, t_ms, pnl, fee=0.05, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def _view_with_signal(coins: list[str]) -> MarketView:
    """Build a view where every given coin has a fresh >2-sigma fade signal."""
    candles = {}
    mids = {}
    vol = {}
    for c in coins:
        # mid far above vwap -> z ~ +3 -> short signal
        candles[c] = {"vwap": 1.00, "sigma": 0.01, "n": 60}
        mids[c] = 1.03
        vol[c] = 50_000_000.0
    return MarketView(
        ts_ms=int(time.time() * 1000),
        mids=mids,
        extra={"candles_1h": candles, "day_ntl_vlm": vol, "live_positions": []},
    )


def test_vetoed_coin_gets_no_place_decision(conn):
    """ADA bled money recently -> no new ADA entry even with a valid signal."""
    now = int(time.time() * 1000)
    # 3 ADA fills, total net ~ -$30 on ~$30 notional -> edge deeply negative.
    for i in range(3):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000,
                     pnl=-10.0, fee=0.1, sz=10.0, px=1.0)

    agent = TwapMrAgent(config={}, conn=conn)
    decisions = agent.decide(_view_with_signal(["ADA"]))

    places = [d for d in decisions if d.action == "place" and d.coin == "ADA"]
    assert places == []


def test_non_vetoed_coin_still_places(conn):
    """A coin with no losing history still gets entered on the same signal."""
    agent = TwapMrAgent(config={}, conn=conn)
    decisions = agent.decide(_view_with_signal(["SOL"]))

    places = [d for d in decisions if d.action == "place" and d.coin == "SOL"]
    assert len(places) == 1


def test_small_loss_below_threshold_not_vetoed(conn):
    """A single tiny loss must not veto (needs min fills + material loss)."""
    now = int(time.time() * 1000)
    _insert_fill(conn, "twap_mr_v1", "SOL", now - 1000, pnl=-0.50, fee=0.05,
                 sz=10.0, px=1.0)

    agent = TwapMrAgent(config={}, conn=conn)
    decisions = agent.decide(_view_with_signal(["SOL"]))

    places = [d for d in decisions if d.action == "place" and d.coin == "SOL"]
    assert len(places) == 1
