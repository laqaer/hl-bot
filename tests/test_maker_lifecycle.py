"""Maker audit state tests with an isolated database and no exchange.

A resting quote has no position ownership. Cancellation clears its working state.
"""

from __future__ import annotations

import pytest

from hl_bot.db.schema import init_db
from hl_bot.exec.maker import (
    log_cancel,
    log_rest,
    working_orders,
)
from hl_bot.exec.orders import bot_owned_coins


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "m.sqlite")


def test_rested_order_is_working_not_owned(conn):
    log_rest(conn, "xfund_carry_v1", "BTC", "B", 0.01, 64000.0, "0xabc1", oid=111)
    working = working_orders(conn, "xfund_carry_v1")
    assert "BTC" in working
    assert working["BTC"]["oid"] == 111
    # not a position until filled
    assert bot_owned_coins(conn, "xfund_carry_v1") == set()


def test_cancel_resolves_working_order(conn):
    log_rest(conn, "xfund_carry_v1", "ETH", "A", 0.5, 3000.0, "0xdef2", oid=222)
    o = working_orders(conn, "xfund_carry_v1")["ETH"]
    log_cancel(conn, "xfund_carry_v1", o)
    assert "ETH" not in working_orders(conn, "xfund_carry_v1")
    assert bot_owned_coins(conn, "xfund_carry_v1") == set()
