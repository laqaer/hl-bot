"""Per-agent scorecards (B6 + B7): funding attribution flows into net_pnl and
Sharpe is computed from daily net PnL and drawdown is reported in dollars."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.metrics import score_agent
from hl_bot.scoring.positions import refresh_attribution

NOW = int(time.time() * 1000)
DAY = 86_400_000

_SEQ = iter(range(1, 10_000))


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def fill(conn, agent, coin, side, px, sz, t_ms, closed_pnl=0.0, fee=0.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
                             closed_pnl, fee, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?, '{}')""",
        (f"h{next(_SEQ)}", next(_SEQ), t_ms, coin, side, px, sz, closed_pnl, fee, agent),
    )


def test_agent_scorecard_includes_attributed_funding(conn):
    fill(conn, "femr_v1", "BTC", "A", 100.0, 1.0, NOW - 6 * DAY, fee=0.05)
    conn.execute(
        "INSERT INTO funding_payments(time_ms, coin, usdc, szi, raw_json) VALUES(?,?,?,?,'{}')",
        (NOW - 5 * DAY, "BTC", 2.5, -1.0),
    )
    fill(conn, "femr_v1", "BTC", "B", 100.0, 1.0, NOW - 4 * DAY, closed_pnl=0.0, fee=0.05)
    refresh_attribution(conn)

    sc = score_agent(conn, "femr_v1", "7d")
    assert sc.funding_pnl == pytest.approx(2.5)   # the strategy's main revenue
    assert sc.net_pnl == pytest.approx(2.5 - 0.1)

    # The exchange-total view stays on _account.
    sc_acct = score_agent(conn, "_account", "7d")
    assert sc_acct.funding_pnl == pytest.approx(2.5)


def test_per_agent_sharpe_and_drawdown_computed(conn):
    # A steady winner over 8 days: positive Sharpe, small drawdown.
    for i in range(8):
        t = NOW - (8 - i) * DAY
        fill(conn, "a1", "BTC", "B", 100.0, 1.0, t, fee=0.01)
        pnl = 1.0 if i != 4 else -0.5     # one losing day
        fill(conn, "a1", "BTC", "A", 100.0, 1.0, t + DAY // 2, closed_pnl=pnl, fee=0.01)
    refresh_attribution(conn)

    sc = score_agent(conn, "a1", "30d")
    assert sc.sharpe is not None and sc.sharpe > 0
    assert sc.max_drawdown is None  # fractional DD is account-only (C5)
    assert sc.max_drawdown_usd is not None and sc.max_drawdown_usd <= 0


def test_sparse_agent_has_no_sharpe(conn):
    fill(conn, "a1", "BTC", "B", 100.0, 1.0, NOW - DAY)
    sc = score_agent(conn, "a1", "30d")
    assert sc.sharpe is None


def test_edge_bps_pairs_straddling_trade_with_entry_notional(conn):
    """V4: a trade opened before the window and closed inside it must use the
    matched entry notional as the edge denominator, not just the exit leg. This
    makes its edge comparable to an identical trade opened and closed inside the
    window."""
    # Intra-window round-trip: nets 10 - 0.2 fees = 9.8.
    fill(conn, "a1", "BTC", "B", 100.0, 1.0, NOW - 20 * DAY, fee=0.1)
    fill(conn, "a1", "BTC", "A", 110.0, 1.0, NOW - 15 * DAY, closed_pnl=10.0, fee=0.1)
    # Straddling round-trip: entry outside 30d window, close inside. Windowed
    # net is 10 - 0.1 close fee = 9.9 (entry fee outside window excluded).
    fill(conn, "a1", "BTC", "B", 100.0, 1.0, NOW - 40 * DAY, fee=0.1)
    fill(conn, "a1", "BTC", "A", 110.0, 1.0, NOW - 10 * DAY, closed_pnl=10.0, fee=0.1)

    sc = score_agent(conn, "a1", "30d")
    # Total windowed net 19.7 against $200 of matched entry notional.
    assert sc.edge_bps == pytest.approx(19.7 / 200.0 * 10_000)
    # Sanity: legacy denominator would have been the exit legs ($220 total) for
    # the in-window notional, giving a lower/inconsistent number.
    assert sc.edge_bps != pytest.approx(19.7 / 220.0 * 10_000)
