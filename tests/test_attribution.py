"""Per-agent funding attribution + Sharpe (B6/B7).

Carry strategies earn funding, not price PnL — so a scorecard that ignores
funding (or only computes Sharpe for the account) judges them on the wrong
numbers, which is exactly what the promotion/confirm gates key off. These tests
pin the fix: funding is attributed to the agent holding the coin at the time, and
per-agent Sharpe is computed from daily PnL.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.scoring.metrics import _agent_funding_payments, score_agent

DAY = 86_400_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "a.sqlite")


def _decision(conn, agent, action, coin, t_ms):
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, coin, is_paper) VALUES(?,?,?,?,0)",
        (t_ms, agent, action, coin),
    )


def _funding(conn, coin, t_ms, usdc):
    conn.execute(
        "INSERT INTO funding_payments(time_ms, coin, usdc, szi, funding_rate, raw_json) VALUES(?,?,?,?,?,?)",
        (t_ms, coin, usdc, 0.0, 0.0, "{}"),
    )


def _fill(conn, agent, coin, t_ms, pnl, fee=0.05, sz=1.0, px=100.0, side="B"):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{coin}{t_ms}{side}", t_ms, t_ms, coin, side, px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_funding_attributed_to_holder(conn):
    now = int(time.time() * 1000)
    # agent A holds BTC from t-10m to t-1m; funding lands at t-5m -> attributed to A.
    _decision(conn, "funding_carry_v1", "place", "BTC", now - 600_000)
    _decision(conn, "funding_carry_v1", "flatten", "BTC", now - 60_000)
    _funding(conn, "BTC", now - 300_000, 2.50)

    pays = _agent_funding_payments(conn, "funding_carry_v1", now - DAY)
    assert sum(s for _, s in pays) == pytest.approx(2.50)
    # an unrelated agent gets none
    assert _agent_funding_payments(conn, "twap_mr_v1", now - DAY) == []

    sc = score_agent(conn, "funding_carry_v1", "24h")
    assert sc.funding_pnl == pytest.approx(2.50)
    assert sc.net_pnl == pytest.approx(2.50)  # no fills yet -> funding is the net


def test_funding_split_between_concurrent_holders(conn):
    now = int(time.time() * 1000)
    for ag in ("xfund_carry_v1", "funding_carry_v1"):
        _decision(conn, ag, "place", "ETH", now - 600_000)
        _decision(conn, ag, "flatten", "ETH", now - 60_000)
    _funding(conn, "ETH", now - 300_000, 4.00)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    assert a == pytest.approx(2.0) and b == pytest.approx(2.0)  # split, no double-count


def test_funding_split_weighted_by_size(conn):
    now = int(time.time() * 1000)
    # Both agents are long ETH via fills before funding lands: A holds 3, B holds 1.
    # Funding should split 3:1 by size, not 1:1 like the decision-log fallback.
    _fill(conn, "xfund_carry_v1", "ETH", now - 600_000, pnl=0.0, sz=3.0)
    _fill(conn, "funding_carry_v1", "ETH", now - 600_000, pnl=0.0, sz=1.0)
    _funding(conn, "ETH", now - 300_000, 4.00)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    assert a == pytest.approx(3.0) and b == pytest.approx(1.0)


def test_funding_size_weight_ignores_closed_holder(conn):
    now = int(time.time() * 1000)
    # A opens 2 then fully closes before funding; B opens 1 and holds.
    _fill(conn, "xfund_carry_v1", "ETH", now - 600_000, pnl=0.0, sz=2.0, side="B")
    _fill(conn, "xfund_carry_v1", "ETH", now - 500_000, pnl=0.0, sz=2.0, side="A")
    _fill(conn, "funding_carry_v1", "ETH", now - 600_000, pnl=0.0, sz=1.0, side="B")
    _funding(conn, "ETH", now - 300_000, 5.00)
    a = sum(s for _, s in _agent_funding_payments(conn, "xfund_carry_v1", now - DAY))
    b = sum(s for _, s in _agent_funding_payments(conn, "funding_carry_v1", now - DAY))
    assert a == pytest.approx(0.0) and b == pytest.approx(5.0)


def test_funding_not_attributed_outside_holding_window(conn):
    now = int(time.time() * 1000)
    _decision(conn, "funding_carry_v1", "place", "SOL", now - 600_000)
    _decision(conn, "funding_carry_v1", "flatten", "SOL", now - 500_000)
    _funding(conn, "SOL", now - 100_000, 1.0)  # after the position closed
    assert _agent_funding_payments(conn, "funding_carry_v1", now - DAY) == []


def test_per_agent_sharpe_from_daily_pnl(conn):
    now = int(time.time() * 1000)
    for i, pnl in enumerate([8.0, 12.0, 10.0, 9.0, 11.0]):  # 5 distinct days, varying
        _fill(conn, "twap_mr_v1", "BTC", now - i * DAY, pnl=pnl)
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.sharpe is not None and sc.sharpe > 0


def test_single_day_has_no_sharpe(conn):
    now = int(time.time() * 1000)
    for i in range(4):
        _fill(conn, "twap_mr_v1", "BTC", now - i * 1000, pnl=5.0)  # all same day
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.sharpe is None


def _equity(conn, t_ms, value):
    conn.execute(
        "INSERT INTO equity_snapshots(ts_ms, account_value, total_margin, "
        "total_ntl_pos, total_raw_usd, withdrawable, raw_json) VALUES(?,?,0,0,0,0,'{}')",
        (t_ms, value),
    )


def test_per_agent_dollar_drawdown(conn):
    now = int(time.time() * 1000)
    # Chronologically (oldest -> newest): +100, -40, +5 -> cumulative 100, 60, 65.
    # Peak 100, trough 60 -> max give-back is -$40. fee=0 for clean arithmetic.
    _fill(conn, "twap_mr_v1", "BTC", now - 2 * DAY, pnl=100.0, fee=0.0)
    _fill(conn, "twap_mr_v1", "BTC", now - 1 * DAY, pnl=-40.0, fee=0.0)
    _fill(conn, "twap_mr_v1", "BTC", now - 0 * DAY, pnl=5.0, fee=0.0)
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.max_drawdown_usd == pytest.approx(-40.0)
    # It needs no capital base, so the fractional max_drawdown stays N/A for a real agent.
    assert sc.max_drawdown is None


def test_only_winning_agent_has_zero_drawdown(conn):
    now = int(time.time() * 1000)
    _fill(conn, "twap_mr_v1", "BTC", now - 1 * DAY, pnl=10.0, fee=0.0)
    _fill(conn, "twap_mr_v1", "BTC", now - 0 * DAY, pnl=20.0, fee=0.0)
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.max_drawdown_usd == pytest.approx(0.0)


def test_no_activity_has_na_drawdown(conn):
    sc = score_agent(conn, "twap_mr_v1", "all")
    assert sc.max_drawdown_usd is None


def test_account_dollar_drawdown_from_equity_curve(conn):
    now = int(time.time() * 1000)
    # Equity 1000 -> 1200 -> 900 -> 1000 across 4 days: peak 1200, trough 900 -> -$300.
    for i, v in enumerate([1000.0, 1200.0, 900.0, 1000.0]):
        _equity(conn, now - (3 - i) * DAY, v)
    sc = score_agent(conn, "_account", "all")
    assert sc.max_drawdown_usd == pytest.approx(-300.0)
