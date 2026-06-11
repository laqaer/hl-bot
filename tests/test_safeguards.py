"""Account-level safeguards behind auto-promotion: order-rate circuit breaker
and live_small mode sizing."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.exec.orders import order_rate_ok
from hl_bot.risk.allocation import AgentCap, apply_mode_sizing
from hl_bot.supervisor.goals import Sizing

NOW = int(time.time() * 1000)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def attempt(conn, agent, t_ms, action="place", is_paper=0):
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, coin, is_paper) VALUES(?,?,?,'BTC',?)",
        (t_ms, agent, action, is_paper),
    )


def test_order_rate_per_agent(conn):
    for i in range(20):
        attempt(conn, "a1", NOW - i * 1000)
    ok, why = order_rate_ok(conn, "a1", max_per_hour=20, now_ms=NOW)
    assert ok is False and "a1" in why
    ok, _ = order_rate_ok(conn, "a2", max_per_hour=20, account_max_per_hour=60, now_ms=NOW)
    assert ok is True


def test_order_rate_account_wide(conn):
    for i in range(60):
        attempt(conn, f"agent{i % 5}", NOW - i * 1000)
    ok, why = order_rate_ok(conn, "fresh_agent", account_max_per_hour=60, now_ms=NOW)
    assert ok is False and "account" in why


def test_order_rate_ignores_paper_and_old_rows(conn):
    for i in range(50):
        attempt(conn, "a1", NOW - i * 1000, is_paper=1)        # paper sim noise
    for i in range(50):
        attempt(conn, "a1", NOW - 2 * 3_600_000 - i * 1000)    # >1h old
    ok, _ = order_rate_ok(conn, "a1", max_per_hour=20, now_ms=NOW)
    assert ok is True


def test_mode_sizing_clamps_live_small():
    cap = AgentCap(max_total_notional=500.0, max_notional_per_trade=100.0)
    sizing = Sizing(live_small_max_total=75, live_small_max_per_trade=25)
    small = apply_mode_sizing(cap, "live_small", sizing)
    assert small.max_total_notional == 75
    assert small.max_notional_per_trade == 25
    # full live and paper are untouched
    assert apply_mode_sizing(cap, "live", sizing) == cap
    assert apply_mode_sizing(cap, "paper", sizing) == cap


def test_mode_sizing_fraction():
    cap = AgentCap(max_total_notional=500.0, max_notional_per_trade=100.0)
    small = apply_mode_sizing(cap, "live_small", Sizing(live_small_fraction=0.2))
    assert small.max_total_notional == pytest.approx(100.0)
    assert small.max_notional_per_trade == pytest.approx(100.0)  # min(per_trade, total)


def test_mode_sizing_never_raises_caps():
    cap = AgentCap(max_total_notional=10.0, max_notional_per_trade=5.0)
    small = apply_mode_sizing(cap, "live_small",
                              Sizing(live_small_max_total=75, live_small_max_per_trade=25))
    assert small.max_total_notional == 10.0
    assert small.max_notional_per_trade == 5.0
