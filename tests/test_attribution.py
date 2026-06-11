"""Per-agent attribution tests — funding (REVIEW C4) and positions replay (M2).

Funding payments are account-level; these tests pin down that they are
attributed to the agent whose fills imply it held the coin when funding was
paid, that the attribution flows through to the live scorecard, and that the
positions table replay handles add / reduce / flip correctly.
"""

from __future__ import annotations

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.attribution import (
    agent_pnl_events,
    attribute_funding,
    daily_pnl_series,
    funding_events_for_agent,
    replay_positions_table,
)
from hl_bot.scoring.metrics import score_agent

T0 = 1_750_000_000_000  # fixed base timestamp (ms)
HOUR = 3_600_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "attr.sqlite")


def _fill(conn, agent, coin, side, sz, px, t_ms, pnl=0.0, fee=0.0, cloid=None):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{coin}{t_ms}", t_ms, t_ms, coin, side, px, sz, 0, "x",
         pnl, fee, "USDC", 0, cloid, agent, "{}"),
    )


def _funding(conn, coin, usdc, t_ms):
    conn.execute(
        """INSERT INTO funding_payments(time_ms, coin, usdc, szi, funding_rate, raw_json)
           VALUES(?,?,?,?,?,?)""",
        (t_ms, coin, usdc, None, None, "{}"),
    )


def test_funding_goes_to_the_holder(conn):
    # carry agent shorts BTC at T0; funding paid at T0+1h while held; closes at
    # T0+2h; funding at T0+3h (flat) must NOT be attributed.
    _fill(conn, "xfund_carry_v1", "BTC", "A", 0.01, 60000, T0)
    _funding(conn, "BTC", 0.50, T0 + HOUR)
    _fill(conn, "xfund_carry_v1", "BTC", "B", 0.01, 59900, T0 + 2 * HOUR, pnl=1.0)
    _funding(conn, "BTC", 0.40, T0 + 3 * HOUR)

    attr = attribute_funding(conn)
    assert attr.get("xfund_carry_v1") == pytest.approx(0.50)

    events = funding_events_for_agent(conn, "xfund_carry_v1")
    assert events == [(T0 + HOUR, pytest.approx(0.50))]


def test_funding_split_proportional_by_size(conn):
    # two agents hold ETH when funding lands -> proportional |size| split
    _fill(conn, "xfund_carry_v1", "ETH", "A", 3.0, 2500, T0)
    _fill(conn, "funding_carry_v1", "ETH", "A", 1.0, 2500, T0)
    _funding(conn, "ETH", 1.00, T0 + HOUR)

    attr = attribute_funding(conn)
    assert attr["xfund_carry_v1"] == pytest.approx(0.75)
    assert attr["funding_carry_v1"] == pytest.approx(0.25)


def test_funding_with_no_replayed_holder_stays_unattributed(conn):
    _funding(conn, "SOL", 2.0, T0)  # no fills at all
    assert attribute_funding(conn) == {}


def test_position_opened_before_window_still_collects_in_window(conn):
    # the timeline is built from ALL fills, only payments are window-filtered
    _fill(conn, "funding_carry_v1", "BTC", "B", 0.02, 60000, T0)
    _funding(conn, "BTC", -0.30, T0 + 10 * HOUR)
    attr = attribute_funding(conn, since_ms=T0 + 5 * HOUR)
    assert attr["funding_carry_v1"] == pytest.approx(-0.30)


def test_scorecard_includes_attributed_funding(conn):
    # REVIEW C4: a carry agent's funding revenue must reach its own net_pnl
    _fill(conn, "xfund_carry_v1", "BTC", "A", 0.01, 60000, T0, fee=0.05)
    _funding(conn, "BTC", 0.80, T0 + HOUR)
    sc = score_agent(conn, "xfund_carry_v1", "all")
    assert sc.funding_pnl == pytest.approx(0.80)
    assert sc.net_pnl == pytest.approx(0.80 - 0.05)
    # account-level total stays the exact funding_payments sum
    acct = score_agent(conn, "_account", "all")
    assert acct.funding_pnl == pytest.approx(0.80)


def test_per_agent_sharpe_and_dollar_drawdown(conn):
    # REVIEW C5: real agents get Sharpe (daily PnL) + dollar drawdown
    day = 86_400_000
    for i, pnl in enumerate([5.0, -2.0, 4.0, -1.0, 3.0]):
        _fill(conn, "funding_carry_v1", "BTC", "B", 0.01, 60000, T0 + i * day,
              pnl=pnl, fee=0.1)
    sc = score_agent(conn, "funding_carry_v1", "all")
    assert sc.sharpe is not None
    assert sc.max_drawdown_usd is not None and sc.max_drawdown_usd <= -2.1
    assert sc.max_drawdown is None  # fractional DD stays account-only


def test_daily_pnl_series_zero_fills_gaps(conn):
    day = 86_400_000
    events = [(T0, 1.0), (T0 + 3 * day, 2.0)]
    daily = daily_pnl_series(events)
    assert len(daily) == 4
    assert daily[0] == pytest.approx(1.0)
    assert daily[1] == daily[2] == 0.0
    assert daily[3] == pytest.approx(2.0)


def test_agent_pnl_events_merge_fills_and_funding(conn):
    _fill(conn, "xfund_carry_v1", "BTC", "A", 0.01, 60000, T0, fee=0.05)
    _funding(conn, "BTC", 0.50, T0 + HOUR)
    _fill(conn, "xfund_carry_v1", "BTC", "B", 0.01, 59900, T0 + 2 * HOUR, pnl=1.0, fee=0.05)
    events = agent_pnl_events(conn, "xfund_carry_v1")
    assert [e[0] for e in events] == [T0, T0 + HOUR, T0 + 2 * HOUR]
    assert sum(e[1] for e in events) == pytest.approx(-0.05 + 0.50 + 0.95)


# ---------------------------------------------------------------------------
# positions table replay (M2)
# ---------------------------------------------------------------------------


def _pos_row(conn, agent, coin):
    return conn.execute(
        "SELECT * FROM positions WHERE agent=? AND coin=?", (agent, coin)
    ).fetchone()


def test_replay_open_add_averages_entry(conn):
    _fill(conn, "femr_v1", "BTC", "B", 1.0, 100.0, T0, fee=0.1)
    _fill(conn, "femr_v1", "BTC", "B", 1.0, 110.0, T0 + HOUR, fee=0.1)
    replay_positions_table(conn)
    r = _pos_row(conn, "femr_v1", "BTC")
    assert r["net_sz"] == pytest.approx(2.0)
    assert r["avg_entry_px"] == pytest.approx(105.0)
    assert r["fees_paid"] == pytest.approx(0.2)


def test_replay_partial_reduce_keeps_entry(conn):
    _fill(conn, "femr_v1", "BTC", "B", 2.0, 100.0, T0)
    _fill(conn, "femr_v1", "BTC", "A", 0.5, 120.0, T0 + HOUR, pnl=10.0)
    replay_positions_table(conn)
    r = _pos_row(conn, "femr_v1", "BTC")
    assert r["net_sz"] == pytest.approx(1.5)
    assert r["avg_entry_px"] == pytest.approx(100.0)
    assert r["realized_pnl"] == pytest.approx(10.0)


def test_replay_flip_resets_entry_to_flip_price(conn):
    _fill(conn, "femr_v1", "ETH", "B", 1.0, 100.0, T0)
    _fill(conn, "femr_v1", "ETH", "A", 3.0, 90.0, T0 + HOUR, pnl=-10.0)
    replay_positions_table(conn)
    r = _pos_row(conn, "femr_v1", "ETH")
    assert r["net_sz"] == pytest.approx(-2.0)
    assert r["avg_entry_px"] == pytest.approx(90.0)


def test_replay_full_close_zeroes_position(conn):
    _fill(conn, "femr_v1", "SOL", "B", 1.0, 100.0, T0)
    _fill(conn, "femr_v1", "SOL", "A", 1.0, 105.0, T0 + HOUR, pnl=5.0)
    n = replay_positions_table(conn)
    assert n == 1
    r = _pos_row(conn, "femr_v1", "SOL")
    assert r["net_sz"] == pytest.approx(0.0)
    assert r["realized_pnl"] == pytest.approx(5.0)
