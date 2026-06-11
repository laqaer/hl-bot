"""Health assessment + heartbeat for unattended 24/7 operation.

``assess_health`` reads the ground-truth DB and returns an ok/warn/down verdict:
  * is the bot still ticking?            (recent agent_decisions)
  * is ingest fresh?                     (recent fills/equity snapshots)
  * is any agent paused by a guardrail?  (agent_state.enabled = 0)
  * is it bleeding?                       (24h realized PnL vs a floor)

The verdict drives a dead-man switch: ``hlbot health`` pings ``HEALTHCHECK_URL``
only when status is ok, so a missed ping (crashed/hung bot) pages you. Anything
worse than ok also fires a Telegram alert. Pure logic here; side effects are thin.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .kill import kill_active

Level = str  # "ok" | "warn" | "crit"


@dataclass
class HealthReport:
    status: str                                   # ok | warn | down
    checks: list[tuple[str, Level, str]] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def render(self) -> str:
        icon = {"ok": "🟢", "warn": "🟡", "down": "🔴"}.get(self.status, "❔")
        lines = [f"{icon} hl-bot health: {self.status.upper()}"]
        for name, level, detail in self.checks:
            mark = {"ok": "✓", "warn": "⚠", "crit": "✗"}.get(level, "?")
            lines.append(f"  {mark} {name}: {detail}")
        return "\n".join(lines)


def _max_ts(conn: sqlite3.Connection, table: str, col: str) -> int | None:
    try:
        row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row and row[0] is not None else None


def assess_health(
    conn: sqlite3.Connection,
    *,
    now_ms: int | None = None,
    max_tick_age_s: int = 900,        # 15 min: ticks should be far more frequent
    max_ingest_age_s: int = 3600,     # 1 h
    daily_loss_floor: float = -1e9,   # set to a negative $ to flag bleeding
    data_dir: str | Path | None = None,
) -> HealthReport:
    now_ms = now_ms or int(time.time() * 1000)
    checks: list[tuple[str, Level, str]] = []
    metrics: dict[str, float | None] = {}

    # --- kill switch (sticky emergency brake) ---
    if data_dir is not None:
        kill_reason = kill_active(data_dir)
        if kill_reason:
            checks.append(("kill", "crit", f"KILL ACTIVE: {kill_reason}"))
        else:
            checks.append(("kill", "ok", "not tripped"))

    # --- tick freshness (is the bot alive?) ---
    last_tick = _max_ts(conn, "agent_decisions", "ts_ms")
    if last_tick is None:
        checks.append(("tick", "warn", "no decisions logged yet"))
        metrics["tick_age_s"] = None
    else:
        age = (now_ms - last_tick) / 1000
        metrics["tick_age_s"] = age
        level = "crit" if age > max_tick_age_s else "ok"
        checks.append(("tick", level, f"last decision {age/60:.1f} min ago"))

    # --- ingest freshness ---
    last_fill = _max_ts(conn, "fills", "time_ms")
    last_eq = _max_ts(conn, "equity_snapshots", "ts_ms")
    last_ingest = max([t for t in (last_fill, last_eq) if t is not None], default=None)
    if last_ingest is None:
        checks.append(("ingest", "warn", "no fills/equity snapshots yet"))
        metrics["ingest_age_s"] = None
    else:
        age = (now_ms - last_ingest) / 1000
        metrics["ingest_age_s"] = age
        level = "warn" if age > max_ingest_age_s else "ok"
        checks.append(("ingest", level, f"last ingest {age/60:.1f} min ago"))

    # --- equity ---
    eq_row = conn.execute(
        "SELECT account_value FROM equity_snapshots ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    equity = float(eq_row[0]) if eq_row and eq_row[0] is not None else None
    metrics["equity"] = equity
    if equity is not None:
        checks.append(("equity", "ok", f"${equity:.2f}"))

    # --- paused agents (guardrail trips) ---
    paused = [r[0] for r in conn.execute(
        "SELECT agent FROM agent_state WHERE enabled = 0"
    ).fetchall()]
    if paused:
        checks.append(("agents", "warn", f"paused: {', '.join(paused)}"))
    else:
        checks.append(("agents", "ok", "none paused"))

    # --- 24h realized PnL ---
    since = now_ms - 86_400_000
    row = conn.execute(
        "SELECT COALESCE(SUM(closed_pnl),0) - COALESCE(SUM(fee),0) FROM fills WHERE time_ms >= ?",
        (since,),
    ).fetchone()
    pnl24 = float(row[0] or 0.0)
    metrics["pnl_24h"] = pnl24
    if pnl24 < daily_loss_floor:
        checks.append(("pnl_24h", "crit", f"${pnl24:+.2f} < floor ${daily_loss_floor:+.2f}"))
    else:
        checks.append(("pnl_24h", "ok", f"${pnl24:+.2f}"))

    levels = [lvl for _, lvl, _ in checks]
    status = "down" if "crit" in levels else ("warn" if "warn" in levels else "ok")
    return HealthReport(status=status, checks=checks, metrics=metrics)


def ping_heartbeat(url: str | None, *, ok: bool = True, timeout: float = 10.0) -> bool:
    """Ping a dead-man-switch URL (e.g. Healthchecks.io). Append '/fail' when not
    ok so the monitor pages immediately. Best-effort; returns whether it sent."""
    if not url:
        return False
    import httpx
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        httpx.get(target, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False
