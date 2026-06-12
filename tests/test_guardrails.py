"""Regression tests for live execution guardrails.

The original guardrail only summed 24h PnL for agents matching `femr%`, so a
bleeding TWAP agent could keep placing orders while the account drained. These
tests pin the corrected behavior: the daily-loss guardrail must aggregate over
ALL active bot agents passed in, and the daily-loss threshold can scale with
portfolio size.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.exec.orders import (
    GuardrailConfig,
    check_guardrails,
    dynamic_daily_loss_limit,
)


class FakeInfo:
    """Minimal stand-in for hyperliquid.info.Info used by check_guardrails."""

    def __init__(self, account_value: float = 1000.0, spot_usdc: float = 0.0,
                 asset_positions: list[dict] | None = None) -> None:
        self._account_value = account_value
        self._spot_usdc = spot_usdc
        self._asset_positions = asset_positions or []

    def user_state(self, address: str) -> dict:
        return {
            "marginSummary": {"accountValue": str(self._account_value)},
            "assetPositions": self._asset_positions,
        }

    def post(self, path: str, body: dict) -> dict:
        # Only spotClearinghouseState is requested via post() in _spot_usdc.
        if body.get("type") == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "total": str(self._spot_usdc)}]}
        return {}


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "guardrails.sqlite")


def _insert_fill(conn, agent, t_ms, pnl, fee=0.1, sz=1.0, px=100.0, coin="BTC"):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_guardrail_halts_on_twap_daily_loss_not_just_femr(conn):
    """A bleeding TWAP agent must trip the 24h loss guardrail even though the
    legacy query only looked at femr%."""
    now = int(time.time() * 1000)
    # TWAP lost $50 in the last 24h; FEMR did nothing.
    _insert_fill(conn, "twap_mr_v1", now - 1000, pnl=-50.0, coin="ADA")

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)

    ok, why = check_guardrails(conn, info, cfg, agents=["femr_v1", "twap_mr_v1"])

    assert ok is False
    assert "pnl" in why.lower() or "loss" in why.lower()


def test_guardrail_passes_when_aggregate_pnl_above_limit(conn):
    now = int(time.time() * 1000)
    # TWAP lost $4, FEMR made $3 -> net -$1, above -$10 limit.
    _insert_fill(conn, "twap_mr_v1", now - 1000, pnl=-4.0, coin="ADA")
    _insert_fill(conn, "femr_v1", now - 1000, pnl=3.0, coin="ETH")

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)

    ok, why = check_guardrails(conn, info, cfg, agents=["femr_v1", "twap_mr_v1"])

    assert ok is True


def _insert_funding(conn, coin, t_ms, usdc):
    conn.execute(
        """INSERT INTO funding_payments(time_ms, coin, usdc, szi, funding_rate, raw_json)
           VALUES(?,?,?,?,?,?)""",
        (t_ms, coin, usdc, 0.0, 0.0, "{}"),
    )


def test_guardrail_halts_on_funding_bleed_without_fills_pnl(conn):
    """A book parked against extreme funding bleeds via funding_payments, not
    fills — the daily-loss guardrail must see it (it used to sum fills only)."""
    now = int(time.time() * 1000)
    # twap holds ADA (open fill, zero closed PnL) and pays $50 funding on it.
    _insert_fill(conn, "twap_mr_v1", now - 7_200_000, pnl=0.0, fee=0.0, coin="ADA")
    _insert_funding(conn, "ADA", now - 3_600_000, -50.0)

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)
    ok, why = check_guardrails(conn, info, cfg, agents=["femr_v1", "twap_mr_v1"])

    assert ok is False
    assert "funding" in why.lower()


def test_guardrail_funding_and_fills_losses_combine(conn):
    """Neither the fills loss nor the funding loss alone trips the limit; the
    honest 24h total does."""
    now = int(time.time() * 1000)
    _insert_fill(conn, "twap_mr_v1", now - 7_200_000, pnl=-6.0, fee=0.0, coin="ADA")
    _insert_funding(conn, "ADA", now - 3_600_000, -6.0)

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)
    ok, why = check_guardrails(conn, info, cfg, agents=["twap_mr_v1"])

    assert ok is False


def test_guardrail_funding_income_never_widens_loss_headroom(conn):
    """Tightening-only: a $50 funding windfall must not mask an $11 fills loss
    that breaches a $10 limit (symmetric inclusion is an operator call)."""
    now = int(time.time() * 1000)
    _insert_fill(conn, "twap_mr_v1", now - 7_200_000, pnl=-11.0, fee=0.0, coin="ADA")
    _insert_funding(conn, "ADA", now - 3_600_000, 50.0)

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)
    ok, why = check_guardrails(conn, info, cfg, agents=["twap_mr_v1"])

    assert ok is False


def test_guardrail_ignores_funding_on_manual_coins(conn):
    """Funding on a coin held only by a manual (unattributed) fill must not
    halt the bot — a human's carry trade is not the bot's loss."""
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"hmanual{now}", now - 7_200_000, now - 7_200_000, "DOGE", "B", 0.1,
         1000.0, 0, "Open Long", 0.0, 0.0, "USDC", 0, None, None, "{}"),
    )
    _insert_funding(conn, "DOGE", now - 3_600_000, -50.0)

    info = FakeInfo(account_value=1000.0)
    cfg = GuardrailConfig(min_bot_capital=40.0, max_daily_loss=10.0,
                          max_total_notional=5000.0)
    ok, why = check_guardrails(conn, info, cfg, agents=["femr_v1", "twap_mr_v1"])

    assert ok is True


def test_dynamic_daily_loss_limit_scales_with_portfolio():
    # Floor applies for tiny accounts.
    assert dynamic_daily_loss_limit(100.0, floor=10.0, pct=0.03) == pytest.approx(10.0)
    # 3% of portfolio applies once it exceeds the floor.
    assert dynamic_daily_loss_limit(1000.0, floor=10.0, pct=0.03) == pytest.approx(30.0)
    # Non-positive / missing portfolio falls back to floor.
    assert dynamic_daily_loss_limit(None, floor=10.0, pct=0.03) == pytest.approx(10.0)
    assert dynamic_daily_loss_limit(0.0, floor=10.0, pct=0.03) == pytest.approx(10.0)
