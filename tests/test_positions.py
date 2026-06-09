"""Fills -> positions replay (B9 / REVIEW M2).

Funding attribution and per-agent accounting infer ownership from the decision
log as a binary flag, which can't see partial fills or size drift. These tests
pin the size-aware replay: net size, size-weighted average entry, exchange
realized PnL, and fees, materialized into the `positions` table.
"""

from __future__ import annotations

import pytest

from hl_bot.db.positions import rebuild_positions, replay_positions
from hl_bot.db.schema import init_db


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "p.sqlite")


def _fill(conn, agent, coin, side, px, sz, t_ms, closed_pnl=0.0, fee=0.05, tid=None):
    tid = t_ms if tid is None else tid
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{coin}{tid}", tid, t_ms, coin, side, px, sz, 0, "dir",
         closed_pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_partial_fills_size_weighted_entry():
    # Two opening buys at different prices -> weighted average entry.
    fills = [
        ("a", "BTC", "B", 100.0, 1.0, 0.0, 0.0, 1),
        ("a", "BTC", "B", 110.0, 3.0, 0.0, 0.0, 2),
    ]
    pos = replay_positions(fills)[("a", "BTC")]
    assert pos.net_sz == pytest.approx(4.0)
    assert pos.avg_entry_px == pytest.approx((100 * 1 + 110 * 3) / 4)  # 107.5


def test_reduction_keeps_entry_and_takes_exchange_pnl():
    # Open 4, sell 1 (partial close). Entry unchanged; realized = exchange pnl.
    fills = [
        ("a", "ETH", "B", 50.0, 4.0, 0.0, 0.0, 1),
        ("a", "ETH", "A", 60.0, 1.0, 9.5, 0.0, 2),  # closed_pnl from exchange
    ]
    pos = replay_positions(fills)[("a", "ETH")]
    assert pos.net_sz == pytest.approx(3.0)
    assert pos.avg_entry_px == pytest.approx(50.0)  # untouched by the reduction
    assert pos.realized_pnl == pytest.approx(9.5)
    assert pos.fees_paid == pytest.approx(0.0)  # fees default 0 here


def test_full_close_resets_entry():
    fills = [
        ("a", "SOL", "B", 20.0, 2.0, 0.0, 0.0, 1),
        ("a", "SOL", "A", 25.0, 2.0, 10.0, 0.0, 2),
    ]
    pos = replay_positions(fills)[("a", "SOL")]
    assert pos.net_sz == pytest.approx(0.0)
    assert pos.avg_entry_px == pytest.approx(0.0)
    assert pos.realized_pnl == pytest.approx(10.0)


def test_flip_through_zero_rebases_entry():
    # Long 1 @100, then sell 3 @90 -> net short 2, entry re-bases to 90.
    fills = [
        ("a", "BTC", "B", 100.0, 1.0, 0.0, 0.0, 1),
        ("a", "BTC", "A", 90.0, 3.0, -10.0, 0.0, 2),
    ]
    pos = replay_positions(fills)[("a", "BTC")]
    assert pos.net_sz == pytest.approx(-2.0)
    assert pos.avg_entry_px == pytest.approx(90.0)


def test_fees_accumulate_across_agents_and_coins():
    fills = [
        ("a", "BTC", "B", 100.0, 1.0, 0.0, 0.1, 1),
        ("a", "ETH", "B", 50.0, 1.0, 0.0, 0.2, 2),
        ("b", "BTC", "B", 100.0, 1.0, 0.0, 0.3, 3),
    ]
    out = replay_positions(fills)
    assert set(out) == {("a", "BTC"), ("a", "ETH"), ("b", "BTC")}
    assert out[("a", "BTC")].fees_paid == pytest.approx(0.1)
    assert out[("b", "BTC")].fees_paid == pytest.approx(0.3)


def test_rebuild_positions_writes_table_and_is_idempotent(conn):
    _fill(conn, "a", "BTC", "B", 100.0, 2.0, 1000, fee=0.1)
    _fill(conn, "a", "BTC", "A", 110.0, 1.0, 2000, closed_pnl=10.0, fee=0.05)
    n = rebuild_positions(conn)
    assert n == 1
    row = conn.execute(
        "SELECT net_sz, avg_entry_px, realized_pnl, fees_paid, last_update_ms FROM positions"
    ).fetchone()
    assert row["net_sz"] == pytest.approx(1.0)
    assert row["avg_entry_px"] == pytest.approx(100.0)
    assert row["realized_pnl"] == pytest.approx(10.0)
    assert row["fees_paid"] == pytest.approx(0.15)
    assert row["last_update_ms"] == 2000

    # Re-running with no new fills yields the same single row (no drift/dupes).
    assert rebuild_positions(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_rebuild_skips_null_agent_fills(conn):
    # A manual fill with NULL agent shouldn't create a phantom position row.
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES('h0',1,1000,'BTC','B',100.0,1.0,0,'dir',0,0,'USDC',0,NULL,NULL,'{}')""",
    )
    assert rebuild_positions(conn) == 0
