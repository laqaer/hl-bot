"""Maker execution lifecycle tests (pure logic, in-memory DB, no exchange).

The cross-tick state machine is the risky part of maker execution, so it's pinned
down here: a rested order is not owned; it becomes owned only when its cloid fills;
it can be cancelled when stale; and ownership keys off 'place', not 'rest'.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.exec.maker import (
    log_cancel,
    log_rest,
    reconcile_maker_fills,
    stale_working,
    working_orders,
)
from hl_bot.exec.orders import bot_owned_coins
from hl_bot.ingest.hyperliquid import ingest_ws_user_fills


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "m.sqlite")


def _fill(conn, agent, coin, cloid, px=100.0, sz=1.0, t_ms=None):
    t_ms = t_ms or int(time.time() * 1000)
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{cloid}", t_ms, t_ms, coin, "B", px, sz, 0, "Open Long",
         0.0, 0.05, "USDC", 0, cloid, agent, "{}"),
    )


def test_rested_order_is_working_not_owned(conn):
    log_rest(conn, "xfund_carry_v1", "BTC", "B", 0.01, 64000.0, "0xabc1", oid=111)
    working = working_orders(conn, "xfund_carry_v1")
    assert "BTC" in working
    assert working["BTC"]["oid"] == 111
    # not a position until filled
    assert bot_owned_coins(conn, "xfund_carry_v1") == set()


def test_fill_detection_makes_it_owned(conn):
    log_rest(conn, "xfund_carry_v1", "BTC", "B", 0.01, 64000.0, "0xabc1", oid=111)
    _fill(conn, "xfund_carry_v1", "BTC", "0xabc1", px=63995.0, sz=0.01)

    filled = reconcile_maker_fills(conn, "xfund_carry_v1", working_orders(conn, "xfund_carry_v1"))
    assert filled == ["BTC"]
    # now owned, and no longer a working order (place resolved the rest)
    assert "BTC" in bot_owned_coins(conn, "xfund_carry_v1")
    assert "BTC" not in working_orders(conn, "xfund_carry_v1")


def test_cancel_resolves_working_order(conn):
    log_rest(conn, "xfund_carry_v1", "ETH", "A", 0.5, 3000.0, "0xdef2", oid=222)
    o = working_orders(conn, "xfund_carry_v1")["ETH"]
    log_cancel(conn, "xfund_carry_v1", o)
    assert "ETH" not in working_orders(conn, "xfund_carry_v1")
    assert bot_owned_coins(conn, "xfund_carry_v1") == set()


def test_ws_user_fill_makes_resting_order_owned_same_tick(conn):
    """A maker quote that fills is detected from the WS snapshot, NOT next REST."""
    log_rest(conn, "xfund_carry_v1", "BTC", "B", 0.01, 64000.0, "0xabc1", oid=111)
    now = int(time.time() * 1000)
    ws_fills = [
        {"hash": "0xh1", "tid": 1, "time": now, "coin": "BTC", "side": "B",
         "px": "63995", "sz": "0.01", "cloid": "0xabc1"},
    ]
    n = ingest_ws_user_fills(conn, ws_fills)
    assert n == 1
    # same WS fill seen again (or via REST) is deduped by (hash,tid)
    assert ingest_ws_user_fills(conn, ws_fills) == 0

    filled = reconcile_maker_fills(conn, "xfund_carry_v1", working_orders(conn, "xfund_carry_v1"))
    assert filled == ["BTC"]
    assert "BTC" in bot_owned_coins(conn, "xfund_carry_v1")
    # real fill px recorded from the WS fill, not the resting quote px
    owned = conn.execute(
        "SELECT px FROM agent_decisions WHERE agent=? AND action='place' AND coin='BTC'",
        ("xfund_carry_v1",),
    ).fetchone()
    assert owned["px"] == 63995.0


def test_stale_detection(conn):
    old = int(time.time() * 1000) - 4000 * 1000  # ~66 min ago
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px,
           cloid, reasoning, market_snapshot, is_paper)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (old, "xfund_carry_v1", "rest", "SOL", "B", 1.0, 150.0, "0xstale",
         "old", '{"oid": 333, "resting": true}', 0),
    )
    working = working_orders(conn, "xfund_carry_v1")
    stale = stale_working(working, max_rest_s=1800)
    assert [o["coin"] for o in stale] == ["SOL"]
    # a fresh one is not stale
    log_rest(conn, "xfund_carry_v1", "BTC", "B", 0.01, 64000.0, "0xfresh", oid=444)
    fresh = working_orders(conn, "xfund_carry_v1")
    assert "BTC" not in [o["coin"] for o in stale_working(fresh, max_rest_s=1800)]
