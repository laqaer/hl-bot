import math
import time

import pytest

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.db.schema import init_db
from hl_bot.risk.scaling import compute_notional_cap, spot_usdc_from_state, unified_portfolio_value


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "risk.sqlite")


def _insert_equity(conn, ts_ms: int, account_value: float):
    conn.execute(
        """INSERT INTO equity_snapshots(
            ts_ms, account_value, total_margin, total_ntl_pos,
            total_raw_usd, withdrawable, cross_leverage, raw_json
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (ts_ms, account_value, 0.0, 0.0, account_value, account_value, None, "{}"),
    )


def test_notional_cap_prefers_live_unified_portfolio_and_has_no_fixed_ceiling(conn):
    cap = compute_notional_cap(conn, live_portfolio_value=600.0)

    assert cap.portfolio_value == pytest.approx(600.0)
    assert cap.max_total_notional == pytest.approx(3000.0)
    assert cap.max_per_position_notional == pytest.approx(600.0)
    assert cap.ceiling_notional is None
    assert cap.source == "live_portfolio_value"
    assert cap.sample_count == 1


def test_notional_cap_can_still_be_emergency_clamped_when_ceiling_supplied(conn):
    cap = compute_notional_cap(conn, live_portfolio_value=600.0, ceiling_notional=1000.0)

    assert cap.max_total_notional == pytest.approx(1000.0)
    assert cap.max_per_position_notional == pytest.approx(600.0)
    assert cap.ceiling_notional == pytest.approx(1000.0)


def test_notional_cap_falls_back_to_5x_trailing_average_when_no_live_portfolio(conn):
    now_ms = int(time.time() * 1000)
    day_ms = 86_400_000
    for i, account_value in enumerate([300.0, 320.0, 340.0]):
        _insert_equity(conn, now_ms - i * day_ms, account_value)

    cap = compute_notional_cap(conn, now_ms=now_ms)

    assert cap.avg_account_value == pytest.approx(320.0)
    assert cap.portfolio_value == pytest.approx(320.0)
    assert cap.max_total_notional == pytest.approx(1600.0)
    assert cap.max_per_position_notional == pytest.approx(320.0)
    assert cap.source == "equity_snapshots"
    assert cap.sample_count == 3


def test_notional_cap_ignores_stale_snapshots_and_uses_live_fallback_alias(conn):
    now_ms = int(time.time() * 1000)
    _insert_equity(conn, now_ms - 10 * 86_400_000, 10_000.0)

    cap = compute_notional_cap(conn, now_ms=now_ms, live_account_value=150.0)

    assert cap.portfolio_value == pytest.approx(150.0)
    assert cap.max_total_notional == pytest.approx(750.0)
    assert cap.max_per_position_notional == pytest.approx(150.0)
    assert cap.source == "live_account_value"
    assert cap.sample_count == 1


def test_notional_cap_is_zero_without_any_capital_source(conn):
    now_ms = int(time.time() * 1000)

    cap = compute_notional_cap(conn, now_ms=now_ms)

    assert cap.portfolio_value is None
    assert cap.avg_account_value is None
    assert cap.max_total_notional == pytest.approx(0.0)
    assert cap.max_per_position_notional == pytest.approx(0.0)
    assert cap.source == "unavailable"
    assert cap.sample_count == 0


def test_unified_portfolio_value_adds_perp_account_value_and_spot_usdc():
    clearinghouse = {"marginSummary": {"accountValue": "236.11"}}
    spot = {"balances": [{"coin": "USDC", "total": "361.70"}, {"coin": "BTC", "total": "0.01"}]}

    assert spot_usdc_from_state(spot) == pytest.approx(361.70)
    assert unified_portfolio_value(clearinghouse, spot) == pytest.approx(597.81)


def test_twap_defaults_have_no_static_total_ceiling_before_live_dynamic_override():
    agent = TwapMrAgent(config={})

    assert agent.cfg.max_notional_per_trade == pytest.approx(200.0)
    assert math.isinf(agent.cfg.max_total_notional)
    assert agent.cfg.max_concurrent_positions == 5
