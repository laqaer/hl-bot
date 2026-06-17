"""Tests for ops automation: health assessment + doctor preflight (offline)."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.ops.doctor import render, run_doctor
from hl_bot.ops.health import assess_health


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "h.sqlite")


def _decision(conn, t_ms, agent="twap_mr_v1"):
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, reasoning, is_paper) VALUES(?,?,?,?,1)",
        (t_ms, agent, "hold", "tick"),
    )


def _fill(conn, t_ms, pnl=1.0, fee=0.05):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, start_position,
           dir, closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{t_ms}", t_ms, t_ms, "BTC", "B", 100.0, 1.0, 0, "Close Long",
         pnl, fee, "USDC", 0, None, "twap_mr_v1", "{}"),
    )


def _equity(conn, t_ms, val=1000.0):
    conn.execute(
        """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
           total_ntl_pos, total_raw_usd, withdrawable, cross_leverage, raw_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (t_ms, val, 0, 0, val, val, None, "{}"),
    )


def test_health_ok_when_fresh(conn):
    now = int(time.time() * 1000)
    _decision(conn, now - 60_000)       # 1 min ago
    _fill(conn, now - 120_000)
    _equity(conn, now - 60_000)
    rep = assess_health(conn, now_ms=now)
    assert rep.status == "ok"
    assert rep.metrics["equity"] == 1000.0


def test_health_down_when_tick_stale(conn):
    now = int(time.time() * 1000)
    _decision(conn, now - 3600_000)     # 1h ago, > 15 min threshold
    rep = assess_health(conn, now_ms=now, max_tick_age_s=900)
    assert rep.status == "down"


def test_health_warn_when_agent_paused(conn):
    now = int(time.time() * 1000)
    _decision(conn, now - 60_000)
    _fill(conn, now - 60_000)
    _equity(conn, now - 60_000)
    conn.execute("INSERT INTO agent_state(agent, mode, enabled) VALUES('twap_mr_v1','paper',0)")
    rep = assess_health(conn, now_ms=now)
    assert rep.status == "warn"
    assert any("paused" in d for _, _, d in rep.checks)


def test_health_down_when_bleeding(conn):
    now = int(time.time() * 1000)
    _decision(conn, now - 60_000)
    _fill(conn, now - 60_000, pnl=-50.0)
    _equity(conn, now - 60_000)
    rep = assess_health(conn, now_ms=now, daily_loss_floor=-30.0)
    assert rep.status == "down"


def test_doctor_ready_with_valid_setup(conn, tmp_path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "x.yaml").write_text("agent: x\nmode: paper\n")
    checks = run_doctor(
        hl_address="0xabc", trader_address="0xabc",
        api_url="https://api.hyperliquid.xyz",
        db_path=tmp_path / "d.sqlite", config_dir=cfg,
        api_wallet_path=tmp_path / "nope.env", require_live=False,
    )
    _, ok = render(checks)
    assert ok  # no criticals (missing wallet is warn when not live)


def test_doctor_not_ready_without_address(conn, tmp_path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "x.yaml").write_text("agent: x\nmode: paper\n")
    checks = run_doctor(
        hl_address="", trader_address=None,
        api_url="https://api.hyperliquid.xyz",
        db_path=tmp_path / "d.sqlite", config_dir=cfg,
        api_wallet_path=tmp_path / "nope.env",
    )
    _, ok = render(checks)
    assert not ok


def test_doctor_live_requires_wallet(conn, tmp_path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "x.yaml").write_text("agent: x\nmode: paper\n")
    checks = run_doctor(
        hl_address="0xabc", trader_address="0xabc",
        api_url="https://api.hyperliquid.xyz",
        db_path=tmp_path / "d.sqlite", config_dir=cfg,
        api_wallet_path=tmp_path / "nope.env", require_live=True,
    )
    _, ok = render(checks)
    assert not ok  # wallet warn upgraded to crit under --live
