"""Paper-fill simulator: taker fills, conservative maker fills, funding accrual,
and scoreability through score_agent(source='paper')."""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision
from hl_bot.db.schema import init_db
from hl_bot.scoring.metrics import score_agent
from hl_bot.sim.paper import simulate_cycle

NOW = int(time.time() * 1000)
HOUR = 3_600_000


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def view(ts, mids, funding=None):
    return MarketView(ts_ms=ts, mids=mids, funding=funding or {})


def place(agent, coin, side, sz, px=None):
    return Decision(agent=agent, action="place", coin=coin, side=side, sz=sz, px=px,
                    cloid=f"c-{agent}-{coin}-{side}", is_paper=True)


def flatten(agent, coin):
    return Decision(agent=agent, action="flatten", coin=coin, is_paper=True)


def test_taker_entry_and_exit_pays_costs(conn):
    v1 = view(NOW - 2 * HOUR, {"BTC": 100.0})
    res = simulate_cycle(conn, v1, [place("a1", "BTC", "B", 1.0)], now_ms=NOW - 2 * HOUR)
    assert len(res.fills) == 1
    row = conn.execute("SELECT * FROM paper_fills").fetchone()
    assert row["px"] > 100.0          # buy pays slippage above mid
    assert row["fee"] > 0
    assert row["closed_pnl"] == 0

    v2 = view(NOW, {"BTC": 110.0})
    simulate_cycle(conn, v2, [flatten("a1", "BTC")], now_ms=NOW)
    rows = conn.execute("SELECT * FROM paper_fills ORDER BY id").fetchall()
    assert len(rows) == 2
    exit_row = rows[1]
    assert exit_row["side"] == "A"
    assert exit_row["closed_pnl"] > 0          # rode 100 -> 110
    assert exit_row["closed_pnl"] < 10.0       # minus entry/exit slippage

    # The audit trail the agents replay must exist (is_paper=1).
    audits = conn.execute(
        "SELECT action, is_paper FROM agent_decisions WHERE agent='a1' ORDER BY id"
    ).fetchall()
    assert [a["action"] for a in audits] == ["place", "flatten"]
    assert all(a["is_paper"] == 1 for a in audits)


def test_maker_entry_requires_cross_not_touch(conn):
    v1 = view(NOW - 3 * HOUR, {"BTC": 100.0})
    res = simulate_cycle(conn, v1, [place("a1", "BTC", "B", 1.0, px=99.0)],
                         maker_entries=True, now_ms=NOW - 3 * HOUR)
    assert res.rested and not res.fills
    assert conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0

    # Touch exactly: still NOT filled (conservative).
    v2 = view(NOW - 2 * HOUR, {"BTC": 99.0})
    res = simulate_cycle(conn, v2, [], maker_entries=True, now_ms=NOW - 2 * HOUR)
    assert not res.fills
    assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1

    # Cross below the limit: fills at the limit with maker fee.
    v3 = view(NOW - HOUR, {"BTC": 98.5})
    res = simulate_cycle(conn, v3, [], maker_entries=True, now_ms=NOW - HOUR)
    assert len(res.fills) == 1
    row = conn.execute("SELECT * FROM paper_fills").fetchone()
    assert row["px"] == pytest.approx(99.0)
    assert row["fee"] == pytest.approx(99.0 * 1.0 * 1.0 / 10_000)  # maker 1 bp
    assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_maker_reentry_replaces_resting_quote_not_stacks(conn):
    # Agents re-emit the same entry each cycle while a quote rests (they can't
    # see "rest" audit rows as ownership). Quotes must replace, not stack.
    t0 = NOW - 3 * HOUR
    for i in range(3):
        d = place("a1", "BTC", "B", 1.0, px=99.0 - i)
        d.cloid = f"c-{i}"  # fresh cloid every cycle, like make_cloid()
        simulate_cycle(conn, view(t0 + i, {"BTC": 100.0}), [d],
                       maker_entries=True, now_ms=t0 + i)
    assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 1
    # The surviving quote is the latest one (replace == reprice).
    assert conn.execute("SELECT limit_px FROM paper_orders").fetchone()[0] == 97.0

    # A deep cross fills exactly one order, not three.
    res = simulate_cycle(conn, view(NOW, {"BTC": 90.0}), [],
                         maker_entries=True, now_ms=NOW)
    assert len(res.fills) == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1


def test_funding_skips_flat_gap_on_reopen(conn):
    t0 = NOW - 10 * HOUR
    simulate_cycle(conn, view(t0, {"BTC": 100.0}), [place("a1", "BTC", "B", 1.0)], now_ms=t0)
    t1 = t0 + HOUR
    simulate_cycle(conn, view(t1, {"BTC": 100.0}, {"BTC": 0.0001}), [], now_ms=t1)
    simulate_cycle(conn, view(t1, {"BTC": 100.0}), [flatten("a1", "BTC")], now_ms=t1)

    # Flat for 6h, then reopen; the next accrual must cover only the time
    # since the reopen, not the flat gap.
    t2 = t1 + 6 * HOUR
    simulate_cycle(conn, view(t2, {"BTC": 100.0}), [place("a1", "BTC", "B", 1.0)], now_ms=t2)
    t3 = t2 + 2 * HOUR
    res = simulate_cycle(conn, view(t3, {"BTC": 100.0}, {"BTC": 0.0001}), [], now_ms=t3)
    assert res.funding_rows == 1
    usdc = conn.execute(
        "SELECT usdc FROM paper_funding WHERE agent='a1' ORDER BY time_ms DESC LIMIT 1"
    ).fetchone()[0]
    assert usdc == pytest.approx(-1.0 * 100.0 * 0.0001 * 2.0)  # 2h, not 8h


def test_funding_accrues_hourly_on_open_position(conn):
    t0 = NOW - 5 * HOUR
    simulate_cycle(conn, view(t0, {"BTC": 100.0}), [place("a1", "BTC", "B", 2.0)], now_ms=t0)

    # 30 min later: under an hour, no accrual yet.
    res = simulate_cycle(conn, view(t0, {"BTC": 100.0}, {"BTC": 0.0001}), [],
                         now_ms=t0 + HOUR // 2)
    assert res.funding_rows == 0

    # 2h after entry: accrues. Long position + positive funding pays.
    t2 = t0 + 2 * HOUR
    res = simulate_cycle(conn, view(t2, {"BTC": 100.0}, {"BTC": 0.0001}), [], now_ms=t2)
    assert res.funding_rows == 1
    usdc = conn.execute("SELECT usdc FROM paper_funding").fetchone()[0]
    assert usdc == pytest.approx(-2.0 * 100.0 * 0.0001 * 2.0)  # -sz*px*rate*hours

    # Short positions EARN positive funding.
    simulate_cycle(conn, view(t2, {"ETH": 50.0}), [place("a2", "ETH", "A", 1.0)], now_ms=t2)
    t3 = t2 + HOUR
    simulate_cycle(conn, view(t3, {"ETH": 50.0}, {"ETH": 0.0002}), [], now_ms=t3)
    usdc = conn.execute(
        "SELECT usdc FROM paper_funding WHERE agent='a2'").fetchone()[0]
    assert usdc == pytest.approx(+1.0 * 50.0 * 0.0002 * 1.0)


def test_paper_scorecard_includes_funding(conn):
    t0 = NOW - 10 * HOUR
    simulate_cycle(conn, view(t0, {"BTC": 100.0}), [place("a1", "BTC", "A", 1.0)], now_ms=t0)
    for h in range(1, 9):
        t = t0 + h * HOUR
        simulate_cycle(conn, view(t, {"BTC": 100.0}, {"BTC": 0.0002}), [], now_ms=t)
    simulate_cycle(conn, view(NOW, {"BTC": 100.0}), [flatten("a1", "BTC")], now_ms=NOW)

    sc = score_agent(conn, "a1", "24h", source="paper")
    assert sc.n_trades == 2
    assert sc.funding_pnl > 0          # short collected positive funding
    assert sc.fees_paid > 0
    # Carry round-trip at flat price: net = funding - fees - slippage costs.
    assert sc.net_pnl == pytest.approx(sc.realized_pnl + sc.funding_pnl - sc.fees_paid)


def test_live_scorecard_unaffected_by_paper(conn):
    t0 = NOW - 2 * HOUR
    simulate_cycle(conn, view(t0, {"BTC": 100.0}), [place("a1", "BTC", "B", 1.0)], now_ms=t0)
    sc_live = score_agent(conn, "a1", "24h", source="live")
    assert sc_live.n_trades == 0
