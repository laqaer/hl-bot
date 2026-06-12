"""Health assessment + heartbeat for unattended 24/7 operation.

``assess_health`` reads the ground-truth DB and returns an ok/warn/down verdict:
  * is the tick loop alive?              (recent tick_heartbeats; decision rows
                                          as a warn-only fallback on legacy DBs)
  * is evidence still accumulating?      (decision rows not silent for days)
  * is ingest fresh?                     (recent fills/equity snapshots)
  * is any agent paused by a guardrail?  (agent_state.enabled = 0)
  * is it bleeding?                       (24h realized PnL vs a floor)
  * is the auto-deployer alive/shipping? (``DeploySignals``; warn-only)
  * can a bad verdict reach a human?     (``PagerSignals``; warn-only)

The verdict drives a dead-man switch: ``hlbot health`` pings ``HEALTHCHECK_URL``
only when status is ok, so a missed ping (crashed/hung bot) pages you. Anything
worse than ok also fires a Telegram alert. Pure logic here; side effects are thin.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

Level = str  # "ok" | "warn" | "crit"

# Written by deploy/update.sh beside the DB; names shared with that script.
DEPLOYED_SHA = ".deployed_sha"        # content: sha of the last test-green deploy
UPDATE_HEARTBEAT = ".update_heartbeat"  # touched on every COMPLETED updater run


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


@dataclass
class DeploySignals:
    """Filesystem truth about the auto-deployer (deploy/update.sh)."""

    auto_update: bool                   # HLBOT_AUTO_UPDATE == "1" in this env
    head_sha: str | None                # repo HEAD on disk (fetched, maybe undeployed)
    deployed_sha: str | None            # content of data/.deployed_sha
    update_beat_age_s: float | None     # age of data/.update_heartbeat; None = missing


@dataclass
class PagerSignals:
    """Can a bad verdict actually reach a human? (env truth at call time)"""

    healthcheck_url: bool   # HEALTHCHECK_URL set → dead-man switch wired
    telegram_token: bool    # TG_BOT_TOKEN set (or the Hermes config fallback)


def read_pager_signals(
    env: Mapping[str, str] | None = None,
    *,
    tg_fallback: object = None,
) -> PagerSignals:
    """Resolve which alert channels ``hlbot health`` could actually use.

    Mirrors the send paths: ``ping_heartbeat`` needs HEALTHCHECK_URL;
    ``telegram_alert`` needs TG_BOT_TOKEN or the Hermes config file it falls
    back to (``tg_fallback`` overrides that lookup for tests — the default
    consults the same ``_load_tg_token`` the alert path uses).
    """
    env = os.environ if env is None else env
    tg = bool(env.get("TG_BOT_TOKEN"))
    if not tg:
        if tg_fallback is None:
            with contextlib.suppress(Exception):
                from ..exec.orders import _load_tg_token
                tg = bool(_load_tg_token())
        else:
            tg = bool(tg_fallback())  # type: ignore[operator]
    return PagerSignals(
        healthcheck_url=bool(env.get("HEALTHCHECK_URL")),
        telegram_token=tg,
    )


def _read_git_head(repo_dir: Path) -> str | None:
    """Repo HEAD straight from .git files (no subprocess); None when unreadable.

    The updater ff-merges BEFORE its test gate, so on-disk HEAD advances even
    when the deploy is then refused — HEAD vs .deployed_sha divergence is the
    "fetched but not shipped" signal.
    """
    try:
        git = repo_dir / ".git"
        if git.is_file():  # worktree/submodule indirection
            line = git.read_text().strip()
            if not line.startswith("gitdir:"):
                return None
            git = (repo_dir / line.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None  # detached HEAD holds the sha itself
        ref = head.split(":", 1)[1].strip()
        loose = git / ref
        if loose.is_file():
            return loose.read_text().strip() or None
        packed = git / "packed-refs"
        if packed.is_file():
            for ln in packed.read_text().splitlines():
                parts = ln.strip().split()
                if len(parts) == 2 and not ln.startswith(("#", "^")) and parts[1] == ref:
                    return parts[0]
        return None
    except OSError:
        return None


def read_deploy_signals(
    db_path: Path | str,
    *,
    now_ms: int | None = None,
    env: Mapping[str, str] | None = None,
) -> DeploySignals:
    """Gather the deploy-freshness inputs, anchored at the DB's data dir.

    update.sh writes ``data/.deployed_sha`` on each test-green deploy and
    touches ``data/.update_heartbeat`` on every completed run, so both sit
    beside the DB regardless of where ``hlbot health`` is invoked from; the
    repo root is the data dir's parent (HLBOT_HOME/data/hlbot.sqlite).
    """
    env = os.environ if env is None else env
    now_ms = now_ms or int(time.time() * 1000)
    data_dir = Path(db_path).parent
    deployed: str | None = None
    with contextlib.suppress(OSError):
        deployed = (data_dir / DEPLOYED_SHA).read_text().strip() or None
    beat_age: float | None = None
    with contextlib.suppress(OSError):
        beat_age = now_ms / 1000 - (data_dir / UPDATE_HEARTBEAT).stat().st_mtime
    return DeploySignals(
        auto_update=env.get("HLBOT_AUTO_UPDATE") == "1",
        head_sha=_read_git_head(data_dir.parent),
        deployed_sha=deployed,
        update_beat_age_s=beat_age,
    )


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
    max_decision_age_s: int = 259_200,  # 3 d: loop alive but book silent → warn
    daily_loss_floor: float = -1e9,   # set to a negative $ to flag bleeding
    deploy: DeploySignals | None = None,
    max_update_age_s: int = 7200,     # 2 h ≈ 8 missed 15-min updater fires
    pager: PagerSignals | None = None,
) -> HealthReport:
    now_ms = now_ms or int(time.time() * 1000)
    checks: list[tuple[str, Level, str]] = []
    metrics: dict[str, float | None] = {}

    # --- tick freshness (is the loop alive?) ---
    # Keyed on tick_heartbeats: one row per COMPLETED femr_tick, paper or live.
    # Decision rows cannot carry this check — ticks log no holds, so
    # agent_decisions grows only when an order/error happens, and a healthy but
    # quiet book reads exactly like a dead loop (15 trade-free minutes used to
    # page the operator; a muted pager is a dead dead-man switch).
    last_beat = _max_ts(conn, "tick_heartbeats", "ts_ms")
    last_decision = _max_ts(conn, "agent_decisions", "ts_ms")
    if last_beat is not None:
        age = (now_ms - last_beat) / 1000
        metrics["tick_age_s"] = age
        level = "crit" if age > max_tick_age_s else "ok"
        checks.append(("tick", level, f"last tick {age/60:.1f} min ago"))
    elif last_decision is not None:
        # Legacy DB (predates heartbeats): decision age is the only signal.
        # It is event-driven, so stale may just mean quiet — warn, never crit.
        age = (now_ms - last_decision) / 1000
        metrics["tick_age_s"] = age
        level = "warn" if age > max_tick_age_s else "ok"
        checks.append(("tick", level, f"no heartbeats; last decision {age/60:.1f} min ago"))
    else:
        checks.append(("tick", "warn", "no ticks recorded yet"))
        metrics["tick_age_s"] = None

    # --- decision activity (is evidence accumulating?) ---
    # Only meaningful when the loop is beating: a live-but-silent book for days
    # means broken roster/feeds (or a dead market) — either way the paper/live
    # evidence the G1–G3 gates wait on has stalled and nobody would notice.
    if last_beat is not None:
        if last_decision is None:
            checks.append(("activity", "warn", "no decisions logged yet"))
            metrics["decision_age_s"] = None
        else:
            age = (now_ms - last_decision) / 1000
            metrics["decision_age_s"] = age
            level = "warn" if age > max_decision_age_s else "ok"
            checks.append(("activity", level, f"last decision {age/3600:.1f} h ago"))

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

    # --- deploy freshness (is the auto-updater alive and shipping?) ---
    # Two compounding multi-day silent failures motivated this (B-DEPLOY-HB):
    # the updater dead at 203/EXEC (never ran, so it left no trace) and the
    # live book running 4-day-old code while every safety rail sat in git.
    # Warn-only by design: a lagging deploy pages nobody and blocks no tick —
    # it just has to stop being invisible.
    if deploy is not None:
        if not deploy.auto_update:
            checks.append(("deploy", "ok", "auto-update disabled"))
        else:
            metrics["update_beat_age_s"] = deploy.update_beat_age_s
            problems: list[str] = []
            if deploy.update_beat_age_s is None:
                problems.append("updater has never completed a run")
            elif deploy.update_beat_age_s > max_update_age_s:
                problems.append(
                    f"updater last completed {deploy.update_beat_age_s/3600:.1f} h ago")
            if deploy.deployed_sha is None:
                problems.append("no deploy recorded (.deployed_sha missing)")
            elif deploy.head_sha is not None and deploy.head_sha != deploy.deployed_sha:
                # Transient for the minutes a test-gated deploy is in flight;
                # persistent means the gate is refusing (tests red) or the
                # deploy half of the run keeps dying.
                problems.append(
                    f"lags repo: HEAD {deploy.head_sha[:8]} vs deployed "
                    f"{deploy.deployed_sha[:8]}")
            if problems:
                checks.append(("deploy", "warn", "; ".join(problems)))
            else:
                detail = f"at {deploy.deployed_sha[:8]}"
                if deploy.update_beat_age_s is not None:
                    detail += f", updater ran {deploy.update_beat_age_s/60:.1f} min ago"
                checks.append(("deploy", "ok", detail))

    # --- pager reachability (can any verdict here reach a human?) ---
    # The Jun-12 incidents were detected in-DB but alerted nobody: the live
    # box ran with HEALTHCHECK_URL/TG_* all empty, so DOWN verdicts died in
    # the journal. Warn-only, and gated to boxes that have actually ticked —
    # a dev clone running `hlbot health` ad hoc never needed a pager.
    if pager is not None and (last_beat is not None or last_decision is not None):
        wired = [name for name, on in
                 (("dead-man URL", pager.healthcheck_url),
                  ("telegram", pager.telegram_token)) if on]
        metrics["pager_channels"] = float(len(wired))
        if not wired:
            checks.append(("pager", "warn",
                           "DOWN pages nobody — set HEALTHCHECK_URL and/or "
                           "TG_BOT_TOKEN"))
        elif pager.healthcheck_url:
            checks.append(("pager", "ok", " + ".join(wired)))
        else:
            # Telegram fires only when a *running* health check goes DOWN; a
            # fully dead box (loop/timer gone) sends nothing — only a missed
            # dead-man ping catches that. Operator's call, so ok not warn.
            checks.append(("pager", "ok",
                           "telegram only — a fully dead box pages nobody "
                           "(no HEALTHCHECK_URL dead-man switch)"))

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
