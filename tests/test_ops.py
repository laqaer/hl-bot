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


def _beat(conn, t_ms, mode="paper"):
    conn.execute(
        "INSERT INTO tick_heartbeats(ts_ms, mode, agents, decisions) VALUES(?,?,?,?)",
        (t_ms, mode, 3, 5),
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
    _beat(conn, now - 60_000)           # loop beat 1 min ago
    _decision(conn, now - 60_000)       # 1 min ago
    _fill(conn, now - 120_000)
    _equity(conn, now - 60_000)
    rep = assess_health(conn, now_ms=now)
    assert rep.status == "ok"
    assert rep.metrics["equity"] == 1000.0


def test_health_down_when_tick_stale(conn):
    # The loop stopped beating — even though the book traded recently. THIS is
    # the dead-loop signal (not decision staleness).
    now = int(time.time() * 1000)
    _beat(conn, now - 3600_000)         # last completed tick 1h ago
    _decision(conn, now - 60_000)       # a (stray) recent decision row
    rep = assess_health(conn, now_ms=now, max_tick_age_s=900)
    assert rep.status == "down"


def test_health_quiet_book_is_not_down(conn):
    # Loop alive, book simply quiet for an hour: pre-heartbeat this paged the
    # operator (decision rows are event-driven — log_holds=False), training
    # them to mute the dead-man switch.
    now = int(time.time() * 1000)
    _beat(conn, now - 60_000)           # tick completed 1 min ago
    _decision(conn, now - 3600_000)     # last order/error 1h ago
    _fill(conn, now - 120_000)
    _equity(conn, now - 60_000)
    rep = assess_health(conn, now_ms=now, max_tick_age_s=900)
    assert rep.status == "ok"


def test_health_legacy_db_stale_decisions_warn_not_page(conn):
    # DB predates tick_heartbeats: decision age is the only signal, and it
    # cannot distinguish quiet from dead — warn, never crit.
    now = int(time.time() * 1000)
    _decision(conn, now - 3600_000)
    rep = assess_health(conn, now_ms=now, max_tick_age_s=900)
    assert rep.status == "warn"
    assert any(name == "tick" and lvl == "warn" for name, lvl, _ in rep.checks)


def test_health_stalled_activity_warns(conn):
    # Loop beating but the book has been silent for days: the evidence the
    # G1-G3 gates wait on has stalled (broken roster/feeds) — surface it.
    now = int(time.time() * 1000)
    _beat(conn, now - 60_000)
    _decision(conn, now - 4 * 86_400_000)   # 4d > 3d default
    _fill(conn, now - 120_000)
    _equity(conn, now - 60_000)
    rep = assess_health(conn, now_ms=now)
    assert rep.status == "warn"
    assert any(name == "activity" and lvl == "warn" for name, lvl, _ in rep.checks)


def test_record_tick_heartbeat_feeds_health(conn):
    from hl_bot.agents.runtime import record_tick_heartbeat

    now = int(time.time() * 1000)
    record_tick_heartbeat(conn, mode="live", agents=2, decisions=7, now_ms=now - 30_000)
    row = conn.execute(
        "SELECT ts_ms, mode, agents, decisions FROM tick_heartbeats").fetchone()
    assert tuple(row) == (now - 30_000, "live", 2, 7)
    rep = assess_health(conn, now_ms=now)
    assert any(name == "tick" and lvl == "ok" for name, lvl, _ in rep.checks)


def test_femr_tick_paper_records_heartbeat(monkeypatch, tmp_path):
    # Wiring pin: a completed paper tick must beat — `hlbot health`'s liveness
    # check is keyed on it. (The live path shares the same tested helper.)
    from typer.testing import CliRunner

    import hl_bot.agents.runtime as rt
    from hl_bot.agents.base import MarketView
    from hl_bot.cli.main import app
    from hl_bot.db.schema import connect

    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(rt, "fetch_account_state", lambda *a, **k: rt.AccountState(
        clearinghouse={}, spot_clearinghouse={}, account_value=100.0,
        spot_usdc=0.0, portfolio_value=100.0, withdrawable=100.0))
    monkeypatch.setattr(rt, "build_tick_view", lambda *a, **k: rt.TickView(
        view=MarketView(ts_ms=0, mids={}), vwap_window=60, bars_15m=0, ws=None))
    monkeypatch.setattr(rt, "build_roster", lambda *a, **k: [])

    res = CliRunner().invoke(app, ["femr_tick"])
    assert res.exit_code == 0, res.output
    row = connect(tmp_path / "t.sqlite").execute(
        "SELECT mode, agents, decisions FROM tick_heartbeats").fetchone()
    assert tuple(row) == ("paper", 0, 0)


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
        hl_address="0xabc", api_url="https://api.hyperliquid.xyz",
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
        hl_address="", api_url="https://api.hyperliquid.xyz",
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
        hl_address="0xabc", api_url="https://api.hyperliquid.xyz",
        db_path=tmp_path / "d.sqlite", config_dir=cfg,
        api_wallet_path=tmp_path / "nope.env", require_live=True,
    )
    _, ok = render(checks)
    assert not ok  # wallet warn upgraded to crit under --live
