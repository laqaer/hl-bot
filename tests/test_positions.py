"""Fills -> positions replay (B9 / REVIEW M2).

Attribution must survive partial fills and manual interference. These tests pin
the replay: net size, size-weighted average entry, and accumulated
realized_pnl/fees are derived purely from the exchange's fills, and the
``positions`` table is rebuilt idempotently from that ground truth.
"""

from __future__ import annotations

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.positions import (
    PositionState,
    rebuild_positions,
    replay_positions,
)


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "p.sqlite")


def _fill(conn, agent, coin, t_ms, side, px, sz, closed_pnl=0.0, fee=0.05, tid=None):
    tid = tid if tid is not None else t_ms
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{coin}{t_ms}{tid}", tid, t_ms, coin, side, px, sz, 0,
         "x", closed_pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_single_open_sets_avg_entry():
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.05, "time_ms": 1},
    ])
    st = states[("a", "BTC")]
    assert st.net_sz == pytest.approx(1.0)
    assert st.avg_entry_px == pytest.approx(100.0)
    assert st.fees_paid == pytest.approx(0.05)


def test_partial_fills_weight_average_entry():
    # 1 @ 100 then 3 @ 200 -> net 4, avg = (100 + 600) / 4 = 175.
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
        {"agent": "a", "coin": "BTC", "side": "B", "px": 200.0, "sz": 3.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 2},
    ])
    st = states[("a", "BTC")]
    assert st.net_sz == pytest.approx(4.0)
    assert st.avg_entry_px == pytest.approx(175.0)


def test_partial_close_keeps_avg_and_accumulates_realized():
    # Open 4 @ 100, close 1 (exchange says +20 closed_pnl). Avg entry unchanged.
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 4.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
        {"agent": "a", "coin": "BTC", "side": "A", "px": 120.0, "sz": 1.0,
         "closed_pnl": 20.0, "fee": 0.0, "time_ms": 2},
    ])
    st = states[("a", "BTC")]
    assert st.net_sz == pytest.approx(3.0)
    assert st.avg_entry_px == pytest.approx(100.0)
    assert st.realized_pnl == pytest.approx(20.0)


def test_full_close_flattens_and_clears_entry():
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 2.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
        {"agent": "a", "coin": "BTC", "side": "A", "px": 110.0, "sz": 2.0,
         "closed_pnl": 20.0, "fee": 0.0, "time_ms": 2},
    ])
    st = states[("a", "BTC")]
    assert st.net_sz == 0.0
    assert st.avg_entry_px == 0.0
    assert st.realized_pnl == pytest.approx(20.0)


def test_flip_through_zero_resets_entry_to_fill_px():
    # Long 1 @ 100, then sell 3 @ 120 -> net -2, residual opened at 120.
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
        {"agent": "a", "coin": "BTC", "side": "A", "px": 120.0, "sz": 3.0,
         "closed_pnl": 20.0, "fee": 0.0, "time_ms": 2},
    ])
    st = states[("a", "BTC")]
    assert st.net_sz == pytest.approx(-2.0)
    assert st.avg_entry_px == pytest.approx(120.0)


def test_distinct_agents_and_coins_are_separate():
    states = replay_positions([
        {"agent": "a", "coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
        {"agent": "manual", "coin": "BTC", "side": "B", "px": 100.0, "sz": 2.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 2},
        {"agent": "a", "coin": "ETH", "side": "A", "px": 50.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 3},
    ])
    assert states[("a", "BTC")].net_sz == pytest.approx(1.0)
    assert states[("manual", "BTC")].net_sz == pytest.approx(2.0)
    assert states[("a", "ETH")].net_sz == pytest.approx(-1.0)


def test_rebuild_positions_is_idempotent(conn):
    _fill(conn, "a", "BTC", 1, "B", 100.0, 2.0, closed_pnl=0.0, fee=0.10)
    _fill(conn, "a", "BTC", 2, "A", 110.0, 1.0, closed_pnl=10.0, fee=0.05)
    n1 = rebuild_positions(conn)
    n2 = rebuild_positions(conn)  # second run must not double-count
    assert n1 == n2 == 1
    row = conn.execute(
        "SELECT net_sz, realized_pnl, fees_paid FROM positions WHERE agent='a' AND coin='BTC'"
    ).fetchone()
    assert row["net_sz"] == pytest.approx(1.0)
    assert row["realized_pnl"] == pytest.approx(10.0)
    assert row["fees_paid"] == pytest.approx(0.15)


def test_rebuild_orders_by_time_not_insertion(conn):
    # Insert out of chronological order; replay must order by time_ms.
    _fill(conn, "a", "BTC", 2, "B", 200.0, 3.0, fee=0.0, tid=2)
    _fill(conn, "a", "BTC", 1, "B", 100.0, 1.0, fee=0.0, tid=1)
    rebuild_positions(conn)
    row = conn.execute(
        "SELECT net_sz, avg_entry_px FROM positions WHERE agent='a' AND coin='BTC'"
    ).fetchone()
    assert row["net_sz"] == pytest.approx(4.0)
    assert row["avg_entry_px"] == pytest.approx(175.0)


def test_null_agent_falls_back_to_manual():
    states = replay_positions([
        {"agent": None, "coin": "BTC", "side": "B", "px": 100.0, "sz": 1.0,
         "closed_pnl": 0.0, "fee": 0.0, "time_ms": 1},
    ])
    assert isinstance(states[("manual", "BTC")], PositionState)
