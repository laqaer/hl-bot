"""Execution-quality telemetry from maker_orders."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.exec_quality import exec_quality

NOW = int(time.time() * 1000)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def mko(conn, *, cloid, agent="a1", state="quoted", created_ms=None,
        updated_ms=None, reprices=0, parent=None):
    created_ms = created_ms or NOW - 3_600_000
    conn.execute(
        """INSERT INTO maker_orders(cloid, agent, coin, side, sz, limit_px, state,
                                    created_ms, updated_ms, reprice_count, parent_cloid)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (cloid, agent, "BTC", "B", 1.0, 100.0, state,
         created_ms, updated_ms or created_ms, reprices, parent),
    )


def test_fill_rate_and_time_to_fill(conn):
    mko(conn, cloid="c1", state="filled",
        created_ms=NOW - 600_000, updated_ms=NOW - 540_000)   # 60s to fill
    mko(conn, cloid="c2", state="expired")
    mko(conn, cloid="c3", state="taker_fallback")
    mko(conn, cloid="c4", state="quoted")
    rep = exec_quality(conn, now_ms=NOW)
    q = rep.per_agent[0]
    assert q.n_quotes == 4
    assert q.fill_rate == pytest.approx(0.25)
    assert q.fallback_rate == pytest.approx(0.25)
    assert q.median_time_to_fill_s == pytest.approx(60.0)


def test_reprice_chain_counts_as_one_quote(conn):
    mko(conn, cloid="c1", state="cancelled")
    mko(conn, cloid="c2", state="filled", parent="c1", reprices=1,
        created_ms=NOW - 300_000, updated_ms=NOW - 200_000)
    rep = exec_quality(conn, now_ms=NOW)
    q = rep.per_agent[0]
    assert q.n_quotes == 1          # one economic quote
    assert q.n_filled == 1
    assert q.fill_rate == pytest.approx(1.0)


def test_alerts_fire_on_low_fill_rate(conn):
    for i in range(10):
        mko(conn, cloid=f"e{i}", state="expired")
    alerts = exec_quality(conn, now_ms=NOW).alerts()
    assert any("fill rate" in a for a in alerts)


def test_no_alerts_below_min_sample(conn):
    mko(conn, cloid="c1", state="expired")
    assert exec_quality(conn, now_ms=NOW).alerts() == []


def test_window_excludes_old_orders(conn):
    mko(conn, cloid="old", state="filled", created_ms=NOW - 48 * 3_600_000)
    rep = exec_quality(conn, window_h=24, now_ms=NOW)
    assert rep.per_agent == []
