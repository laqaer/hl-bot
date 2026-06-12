"""Tests for ops automation: health assessment + doctor preflight (offline)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hl_bot.db.schema import init_db
from hl_bot.ops.doctor import render, run_doctor
from hl_bot.ops.health import (
    DEPLOYED_SHA,
    UPDATE_HEARTBEAT,
    DeploySignals,
    PagerSignals,
    assess_health,
    read_deploy_signals,
    read_pager_signals,
)


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


def _fresh(conn, now):
    """Baseline rows that make every non-deploy check ok."""
    _beat(conn, now - 60_000)
    _decision(conn, now - 60_000)
    _fill(conn, now - 120_000)
    _equity(conn, now - 60_000)


def test_health_deploy_disabled_is_ok(conn):
    # Operator chose no auto-update: visibility line, never a warn.
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, deploy=DeploySignals(
        auto_update=False, head_sha=None, deployed_sha=None, update_beat_age_s=None))
    assert rep.status == "ok"
    assert any(n == "deploy" and lvl == "ok" and "disabled" in d
               for n, lvl, d in rep.checks)


def test_health_deploy_dead_updater_warns(conn):
    # The B-DEPLOY-EXEC shape: updater enabled but never completing a run
    # (203/EXEC left no trace at all) — and the gone-stale variant.
    now = int(time.time() * 1000)
    _fresh(conn, now)
    never = assess_health(conn, now_ms=now, deploy=DeploySignals(
        auto_update=True, head_sha="a" * 40, deployed_sha="a" * 40,
        update_beat_age_s=None))
    assert never.status == "warn"
    assert any(n == "deploy" and lvl == "warn" and "never completed" in d
               for n, lvl, d in never.checks)
    stale = assess_health(conn, now_ms=now, deploy=DeploySignals(
        auto_update=True, head_sha="a" * 40, deployed_sha="a" * 40,
        update_beat_age_s=3 * 3600.0))
    assert stale.status == "warn"
    assert any(n == "deploy" and "3.0 h ago" in d for n, _, d in stale.checks)


def test_health_deploy_lag_warns(conn):
    # Updater alive but refusing to ship (tests red / restart half dying):
    # on-disk HEAD has advanced past the recorded deploy.
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, deploy=DeploySignals(
        auto_update=True, head_sha="beef" * 10, deployed_sha="cafe" * 10,
        update_beat_age_s=120.0))
    assert rep.status == "warn"
    assert any(n == "deploy" and lvl == "warn" and "beefbeef" in d and "cafecafe" in d
               for n, lvl, d in rep.checks)


def test_health_deploy_fresh_ok(conn):
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, deploy=DeploySignals(
        auto_update=True, head_sha="a" * 40, deployed_sha="a" * 40,
        update_beat_age_s=120.0))
    assert rep.status == "ok"
    assert any(n == "deploy" and lvl == "ok" and "aaaaaaaa" in d
               for n, lvl, d in rep.checks)
    assert rep.metrics["update_beat_age_s"] == 120.0


def _make_repo(root: Path, sha: str, *, packed: bool) -> None:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{sha} refs/heads/main\n")
    else:
        (git / "refs" / "heads" / "main").write_text(sha + "\n")


@pytest.mark.parametrize("packed", [False, True])
def test_read_deploy_signals(tmp_path, packed):
    sha = "ab" * 20
    _make_repo(tmp_path, sha, packed=packed)
    data = tmp_path / "data"
    data.mkdir()
    (data / DEPLOYED_SHA).write_text(sha + "\n")
    now_ms = int(time.time() * 1000)
    hb = data / UPDATE_HEARTBEAT
    hb.touch()
    os.utime(hb, (now_ms / 1000 - 300, now_ms / 1000 - 300))
    sig = read_deploy_signals(
        data / "hlbot.sqlite", now_ms=now_ms, env={"HLBOT_AUTO_UPDATE": "1"})
    assert sig.auto_update
    assert sig.head_sha == sha
    assert sig.deployed_sha == sha
    assert sig.update_beat_age_s == pytest.approx(300, abs=5)


def test_read_deploy_signals_missing_everything(tmp_path):
    # No repo, no markers: every field degrades to None, never raises.
    data = tmp_path / "data"
    data.mkdir()
    sig = read_deploy_signals(data / "hlbot.sqlite", env={})
    assert sig == DeploySignals(
        auto_update=False, head_sha=None, deployed_sha=None, update_beat_age_s=None)


def test_health_cli_reports_deploy(monkeypatch, tmp_path):
    # Wiring pin: `hlbot health` must feed real deploy signals into the
    # assessment (fresh box, auto-update on, no markers → the warn the
    # dead-from-birth updater never got).
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "data" / "h.sqlite"))
    monkeypatch.setenv("HLBOT_AUTO_UPDATE", "1")
    res = CliRunner().invoke(app, ["health", "--no-heartbeat"])
    assert res.exit_code == 0, res.output
    assert "deploy" in res.output and "never completed" in res.output


def test_health_pager_unwired_warns(conn):
    # The Jun-12 shape: a ticking box whose DOWN verdicts die in the journal
    # because no alert channel is configured.
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, pager=PagerSignals(
        healthcheck_url=False, telegram_token=False))
    assert rep.status == "warn"
    assert any(n == "pager" and lvl == "warn" and "pages nobody" in d
               for n, lvl, d in rep.checks)
    assert rep.metrics["pager_channels"] == 0.0


def test_health_pager_wired_ok(conn):
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, pager=PagerSignals(
        healthcheck_url=True, telegram_token=True))
    assert rep.status == "ok"
    assert any(n == "pager" and lvl == "ok" and d == "dead-man URL + telegram"
               for n, lvl, d in rep.checks)
    assert rep.metrics["pager_channels"] == 2.0


def test_health_pager_telegram_only_notes_dead_man_gap(conn):
    # Telegram fires only from a *running* health check; a fully dead box
    # sends nothing. Visibility (detail), not a nag (still ok).
    now = int(time.time() * 1000)
    _fresh(conn, now)
    rep = assess_health(conn, now_ms=now, pager=PagerSignals(
        healthcheck_url=False, telegram_token=True))
    assert rep.status == "ok"
    assert any(n == "pager" and lvl == "ok" and "fully dead box" in d
               for n, lvl, d in rep.checks)


def test_health_pager_quiet_on_never_ticked_box(conn):
    # A dev/loop clone with an empty DB never needed a pager — no nudge line.
    rep = assess_health(conn, pager=PagerSignals(
        healthcheck_url=False, telegram_token=False))
    assert not any(n == "pager" for n, _, _ in rep.checks)


def test_read_pager_signals():
    # Env wins; the Hermes fallback is consulted only when TG_BOT_TOKEN is
    # unset (injected here so the test ignores the machine's real config).
    sig = read_pager_signals(
        {"HEALTHCHECK_URL": "https://hc.example/ping", "TG_BOT_TOKEN": "t"},
        tg_fallback=lambda: None)
    assert sig == PagerSignals(healthcheck_url=True, telegram_token=True)
    assert read_pager_signals({}, tg_fallback=lambda: "tok").telegram_token
    empty = read_pager_signals({"HEALTHCHECK_URL": ""}, tg_fallback=lambda: None)
    assert empty == PagerSignals(healthcheck_url=False, telegram_token=False)


def test_health_cli_reports_pager(monkeypatch, tmp_path):
    # Wiring pin: `hlbot health` must feed real pager signals — a ticking box
    # with the pager env empty prints the nudge.
    from typer.testing import CliRunner

    from hl_bot.cli.main import app
    from hl_bot.db.schema import init_db

    db = tmp_path / "data" / "h.sqlite"
    c = init_db(db)
    _beat(c, int(time.time() * 1000) - 60_000)
    c.commit()
    monkeypatch.setenv("HLBOT_DB", str(db))
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    res = CliRunner().invoke(app, ["health", "--no-heartbeat"])
    assert res.exit_code == 0, res.output
    assert "pager" in res.output and "pages nobody" in res.output


def test_update_sh_touches_heartbeat():
    # Name pin: update.sh and health.py share the marker filename by string;
    # renaming one side without the other would silently kill the check.
    script = (Path(__file__).parents[1] / "deploy" / "update.sh").read_text()
    assert UPDATE_HEARTBEAT in script
    assert DEPLOYED_SHA in script


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
