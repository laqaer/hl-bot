"""Positions replay (B9) + per-agent funding attribution (B6).

The reconciliation invariant is the load-bearing property:
SUM(funding_attribution.usdc) == SUM(funding_payments.usdc), always — whatever
cannot be attributed to an agent lands on the '_account' residual row.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.positions import (
    attribute_funding,
    peak_gross_notional,
    position_timeline,
    replay_positions,
)

NOW = int(time.time() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


_TID = iter(range(1, 10_000))


def fill(conn, agent, coin, side, px, sz, t_ms, closed_pnl=0.0, fee=0.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
                             closed_pnl, fee, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?, '{}')""",
        (f"h{next(_TID)}", next(_TID), t_ms, coin, side, px, sz, closed_pnl, fee, agent),
    )


def funding(conn, coin, usdc, t_ms, szi=None):
    conn.execute(
        """INSERT INTO funding_payments(time_ms, coin, usdc, szi, raw_json)
           VALUES(?,?,?,?, '{}')""",
        (t_ms, coin, usdc, szi),
    )


def test_replay_positions_basic(conn):
    fill(conn, "a1", "BTC", "B", 100.0, 2.0, NOW - 4 * HOUR, fee=0.1)
    fill(conn, "a1", "BTC", "B", 110.0, 2.0, NOW - 3 * HOUR, fee=0.1)
    fill(conn, "a1", "BTC", "A", 120.0, 1.0, NOW - 2 * HOUR, closed_pnl=15.0, fee=0.1)
    n = replay_positions(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM positions WHERE agent='a1' AND coin='BTC'").fetchone()
    assert row["net_sz"] == pytest.approx(3.0)
    assert row["avg_entry_px"] == pytest.approx(105.0)   # 2@100 + 2@110
    assert row["realized_pnl"] == pytest.approx(15.0)
    assert row["fees_paid"] == pytest.approx(0.3)


def test_replay_handles_flip(conn):
    fill(conn, "a1", "ETH", "B", 100.0, 1.0, NOW - 3 * HOUR)
    fill(conn, "a1", "ETH", "A", 110.0, 3.0, NOW - 2 * HOUR)  # flip to -2
    replay_positions(conn)
    row = conn.execute("SELECT * FROM positions WHERE agent='a1' AND coin='ETH'").fetchone()
    assert row["net_sz"] == pytest.approx(-2.0)
    assert row["avg_entry_px"] == pytest.approx(110.0)   # fresh short entry


def test_position_timeline(conn):
    fill(conn, "a1", "SOL", "B", 10.0, 5.0, NOW - 3 * HOUR)
    fill(conn, "a2", "SOL", "A", 10.0, 2.0, NOW - 2 * HOUR)
    times, snaps = position_timeline(conn, "SOL")
    assert len(times) == 2
    assert snaps[0] == {"a1": 5.0}
    assert snaps[1] == {"a1": 5.0, "a2": -2.0}


def test_funding_attribution_prorates_and_reconciles(conn):
    # a1 long 3, a2 long 1 at payment time -> a1 gets 75%, a2 gets 25%.
    fill(conn, "a1", "BTC", "B", 100.0, 3.0, NOW - 5 * HOUR)
    fill(conn, "a2", "BTC", "B", 100.0, 1.0, NOW - 4 * HOUR)
    funding(conn, "BTC", -8.0, NOW - 3 * HOUR, szi=4.0)   # longs paid 8
    attribute_funding(conn)

    rows = {r["agent"]: r["usdc"] for r in conn.execute(
        "SELECT agent, usdc FROM funding_attribution").fetchall()}
    assert rows["a1"] == pytest.approx(-6.0)
    assert rows["a2"] == pytest.approx(-2.0)
    total = conn.execute("SELECT SUM(usdc) FROM funding_attribution").fetchone()[0]
    assert total == pytest.approx(-8.0)


def test_funding_attribution_sign_aware(conn):
    # a1 long 3, a2 SHORT 1 -> account net long 2 paid funding; the short agent
    # must receive the opposite-sign share.
    fill(conn, "a1", "BTC", "B", 100.0, 3.0, NOW - 5 * HOUR)
    fill(conn, "a2", "BTC", "A", 100.0, 1.0, NOW - 4 * HOUR)
    funding(conn, "BTC", -2.0, NOW - 3 * HOUR, szi=2.0)
    attribute_funding(conn)
    rows = {r["agent"]: r["usdc"] for r in conn.execute(
        "SELECT agent, usdc FROM funding_attribution").fetchall()}
    assert rows["a1"] == pytest.approx(-3.0)   # 3/2 of -2
    assert rows["a2"] == pytest.approx(+1.0)   # -1/2 of -2 (short earns)
    total = conn.execute("SELECT SUM(usdc) FROM funding_attribution").fetchone()[0]
    assert total == pytest.approx(-2.0)


def test_funding_with_manual_position_dilutes_via_szi(conn):
    # Agent long 1, but exchange position (szi) is 4 -> 3 units are manual.
    # Agent gets 1/4 of the payment; residual 3/4 goes to _account.
    fill(conn, "a1", "BTC", "B", 100.0, 1.0, NOW - 5 * HOUR)
    funding(conn, "BTC", -4.0, NOW - 3 * HOUR, szi=4.0)
    attribute_funding(conn)
    rows = {r["agent"]: r["usdc"] for r in conn.execute(
        "SELECT agent, usdc FROM funding_attribution").fetchall()}
    assert rows["a1"] == pytest.approx(-1.0)
    assert rows["_account"] == pytest.approx(-3.0)
    total = conn.execute("SELECT SUM(usdc) FROM funding_attribution").fetchone()[0]
    assert total == pytest.approx(-4.0)


def test_funding_with_no_agent_positions_goes_to_account(conn):
    funding(conn, "DOGE", 1.5, NOW - HOUR, szi=10.0)
    attribute_funding(conn)
    rows = {r["agent"]: r["usdc"] for r in conn.execute(
        "SELECT agent, usdc FROM funding_attribution").fetchall()}
    assert rows == {"_account": pytest.approx(1.5)}


def test_attribution_is_idempotent(conn):
    fill(conn, "a1", "BTC", "B", 100.0, 1.0, NOW - 5 * HOUR)
    funding(conn, "BTC", -1.0, NOW - 3 * HOUR, szi=1.0)
    attribute_funding(conn)
    attribute_funding(conn)
    total = conn.execute("SELECT SUM(usdc) FROM funding_attribution").fetchone()[0]
    assert total == pytest.approx(-1.0)


def test_peak_gross_notional(conn):
    fill(conn, "a1", "BTC", "B", 100.0, 2.0, NOW - 5 * HOUR)   # gross 200
    fill(conn, "a1", "ETH", "B", 50.0, 2.0, NOW - 4 * HOUR)    # gross 300
    fill(conn, "a1", "BTC", "A", 100.0, 2.0, NOW - 3 * HOUR)   # gross 100
    assert peak_gross_notional(conn, "a1", None) == pytest.approx(300.0)
