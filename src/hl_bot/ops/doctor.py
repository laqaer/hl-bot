"""Preflight checks ('doctor') — run before enabling live, and in CI/deploy.

Validates the things that silently break a live deployment: env present, DB
writable, goal configs valid, API-wallet file permissions tight, and (best-effort)
Hyperliquid reachability. Returns structured results so it can gate a deploy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

Level = str  # "ok" | "warn" | "crit"


@dataclass
class Check:
    name: str
    level: Level
    detail: str


def _check_configs(config_dir: Path) -> Check:
    try:
        from ..supervisor.goals import load_goals
        n = 0
        for p in sorted(config_dir.glob("*.yaml")):
            load_goals(p)
            n += 1
        return Check("configs", "ok", f"{n} goal configs valid")
    except Exception as e:  # noqa: BLE001
        return Check("configs", "crit", f"invalid config: {e}")


def _check_db(db_path: Path) -> Check:
    try:
        from ..db.schema import init_db
        conn = init_db(db_path)
        conn.execute("SELECT 1")
        return Check("db", "ok", f"writable at {db_path}")
    except Exception as e:  # noqa: BLE001
        return Check("db", "crit", f"DB not usable: {e}")


def _check_api_wallet(env_path: Path) -> Check:
    if not env_path.exists():
        return Check("api_wallet", "warn", f"no API wallet at {env_path} (paper only)")
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        return Check("api_wallet", "crit", f"{env_path} perms {oct(mode)} too open; chmod 600")
    return Check("api_wallet", "ok", f"present, perms {oct(mode)}")


def _check_hl_reachable(api_url: str) -> Check:
    try:
        import httpx
        r = httpx.post(api_url + "/info", json={"type": "allMids"}, timeout=5)
        r.raise_for_status()
        n = len(r.json() or {})
        return Check("hl_api", "ok", f"reachable ({n} mids)")
    except Exception as e:  # noqa: BLE001
        return Check("hl_api", "warn", f"unreachable: {type(e).__name__} (network/region?)")


def _check_address_match(hl_address: str, trader_address: str | None) -> Check:
    if not trader_address:
        return Check("address_match", "warn",
                     "HL_TRADER_ADDRESS not set (live trading disabled)")
    if hl_address.lower() == trader_address.lower():
        return Check("address_match", "ok", f"HL_ADDRESS == HL_TRADER_ADDRESS ({hl_address})")
    return Check("address_match", "crit",
                 f"HL_ADDRESS ({hl_address}) != HL_TRADER_ADDRESS ({trader_address}); "
                 "split-brain ingest vs trading")


def _check_ws_snapshot(snapshot_path: str | None, max_age_s: float = 90) -> Check:
    if not snapshot_path:
        return Check("ws_snapshot", "warn", "HLBOT_WS_SNAPSHOT not set")
    p = Path(snapshot_path)
    if not p.exists():
        return Check("ws_snapshot", "warn", f"{p} missing (hlbot-ws not running?)")
    age = time.time() - p.stat().st_mtime
    if age > max_age_s:
        return Check("ws_snapshot", "warn", f"stale ({age:.0f}s old)")
    try:
        snap = json.loads(p.read_text())
        updated_ms = snap.get("updated_ms")
        if updated_ms:
            data_age = (time.time() * 1000 - updated_ms) / 1000.0
            return Check("ws_snapshot", "ok",
                         f"fresh ({age:.0f}s file age, {data_age:.0f}s data age), "
                         f"{len(snap.get('mids', {}))} mids")
        return Check("ws_snapshot", "ok", f"fresh ({age:.0f}s file age)")
    except (ValueError, OSError) as e:
        return Check("ws_snapshot", "warn", f"unreadable: {e}")


def _check_run_heartbeat(heartbeat_path: Path, max_age_s: float = 300) -> Check:
    if not heartbeat_path.exists():
        return Check("run_heartbeat", "warn",
                     f"{heartbeat_path} missing (hlbot-run not running?)")
    age = time.time() - heartbeat_path.stat().st_mtime
    if age > max_age_s:
        return Check("run_heartbeat", "warn", f"stale ({age:.0f}s old)")
    return Check("run_heartbeat", "ok", f"fresh ({age:.0f}s old)")


def _check_recent_fills(conn: sqlite3.Connection, window_s: float = 60) -> Check:
    try:
        since = int((time.time() - window_s) * 1000)
        n = conn.execute(
            "SELECT count(*) FROM fills WHERE time_ms >= ?", (since,)
        ).fetchone()[0]
        return Check("recent_fills", "ok" if n else "warn",
                     f"{n} fills in last {window_s:.0f}s")
    except sqlite3.OperationalError as e:
        return Check("recent_fills", "warn", f"table not ready: {e}")


def run_doctor(
    *,
    hl_address: str,
    trader_address: str | None,
    api_url: str,
    db_path: Path,
    config_dir: Path,
    api_wallet_path: Path,
    require_live: bool = False,
) -> list[Check]:
    """Run all preflight checks. With ``require_live`` the api-wallet warning is
    upgraded to critical (you can't go live without it)."""
    from ..db.schema import init_db

    conn = init_db(db_path)
    checks = [
        Check("hl_address", "ok" if hl_address else "crit",
              hl_address or "HL_ADDRESS not set"),
        _check_address_match(hl_address, trader_address),
        _check_configs(config_dir),
        _check_db(db_path),
        _check_api_wallet(api_wallet_path),
        _check_hl_reachable(api_url),
        _check_ws_snapshot(os.environ.get("HLBOT_WS_SNAPSHOT")),
        _check_run_heartbeat(db_path.parent / "run_heartbeat"),
        _check_recent_fills(conn),
    ]
    if require_live:
        for c in checks:
            if c.name == "api_wallet" and c.level == "warn":
                c.level = "crit"
                c.detail += " (required for --live)"
    return checks


def render(checks: list[Check]) -> tuple[str, bool]:
    """Return (text, all_critical_passed)."""
    lines = []
    crit_ok = True
    for c in checks:
        mark = {"ok": "✓", "warn": "⚠", "crit": "✗"}.get(c.level, "?")
        lines.append(f"  {mark} {c.name}: {c.detail}")
        if c.level == "crit":
            crit_ok = False
    head = "🩺 doctor: " + ("READY" if crit_ok else "NOT READY (criticals failing)")
    return head + "\n" + "\n".join(lines), crit_ok
