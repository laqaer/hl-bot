"""Health assessment + heartbeat for unattended 24/7 operation.

``assess_health`` reads the ground-truth DB and returns an ok/warn/down verdict:
  * is the tick loop alive?              (recent tick_heartbeats; decision rows
                                          as a warn-only fallback on legacy DBs)
  * is evidence still accumulating?      (decision rows not silent for days)
  * is ingest fresh?                     (recent fills/equity snapshots)
  * is any agent paused by a guardrail?  (agent_state.enabled = 0)
  * is it bleeding?                       (24h realized BOT PnL vs a floor;
                                          the shared account's manual fills
                                          print beside it, never judged)
  * is the auto-deployer alive/shipping? (``DeploySignals``; warn-only)
  * can a bad verdict reach a human?     (``PagerSignals``; warn-only)
  * is the paper forward-test loop alive? (``PaperSignals``; warn-only)
  * is the armed store backup landing?   (``BackupSignals``; warn-only)

The verdict drives a dead-man switch: ``hlbot health`` pings ``HEALTHCHECK_URL``
only when status is ok, so a missed ping (crashed/hung bot) pages you. Anything
worse than ok also fires a Telegram alert. Pure logic here; side effects are thin.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

Level = str  # "ok" | "warn" | "crit"

# Written by deploy/update.sh beside the DB; names shared with that script.
DEPLOYED_SHA = ".deployed_sha"        # content: sha of the last test-green deploy
UPDATE_HEARTBEAT = ".update_heartbeat"  # touched on every COMPLETED updater run

# Default paper-DB basename + override env; names shared with
# deploy/run-paper-tick.sh (pinned by test, like the updater markers above).
PAPER_DB_BASENAME = "hlbot_paper.sqlite"
PAPER_DB_ENV = "HLBOT_PAPER_DB"

# Bleeding floor for the pnl_24h check: unarmed by default (nothing trips a
# −1e9 floor); armed via `hlbot health --daily-loss-floor` or the env below.
DAILY_LOSS_FLOOR_ENV = "HLBOT_DAILY_LOSS_FLOOR"
UNARMED_LOSS_FLOOR = -1e9


def resolve_daily_loss_floor(
    cli_value: float | None = None,
    env: Mapping[str, str] | None = None,
) -> float:
    """Resolve the bleeding floor: CLI > HLBOT_DAILY_LOSS_FLOOR > unarmed.

    A malformed env value raises instead of falling through — silently
    disarming a safety rail is worse than crashing: `hlbot health` then exits
    non-zero, the dead-man ping is missed, and the typo pages a human.
    """
    if cli_value is not None:
        return cli_value
    raw = (os.environ if env is None else env).get(DAILY_LOSS_FLOOR_ENV, "").strip()
    if not raw:
        return UNARMED_LOSS_FLOOR
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(
            f"{DAILY_LOSS_FLOOR_ENV}={raw!r} is not a number — refusing to "
            "run with a silently disarmed bleeding floor"
        ) from e


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


@dataclass
class PaperSignals:
    """Is the paper forward-test loop (run-paper-tick.sh) actually running?"""

    present: bool                   # a separate paper DB exists beside this one
    beat_age_s: float | None        # age of its newest paper tick_heartbeat
    empty_feeds: dict[str, float] = field(default_factory=dict)  # feed → hours empty


@dataclass
class BackupSignals:
    """Is the armed off-host store backup (B-STOREBKP) actually succeeding?"""

    armed: bool                       # HLBOT_STORE_BACKUP_S3 set in this env
    last_success_age_s: float | None  # age of the last upload marker; None = never
    target: str | None = None         # bucket[/prefix], for the detail line


def read_backup_signals(
    *,
    now_ms: int | None = None,
    env: Mapping[str, str] | None = None,
    store_root: Path | str | None = None,
) -> BackupSignals:
    """Read the off-host store backup's last-success marker, if armed.

    ``backup_store`` warns in the journal only, so an armed box whose uploads
    started failing (perms drift, deleted bucket) loses its one off-host copy
    of the irreplaceable 1m sample with zero signal (B-STOREBKP2). This reads
    the ``.candle_backup_state.json`` marker the uploader writes beside the
    store — same env gate, same path resolution, so reader and writer cannot
    diverge. Unarmed (env unset) ⇒ ``armed=False`` and no check is emitted;
    a missing/unreadable marker on an armed box reads as "never succeeded".
    """
    from ..backtest.store import store_dir
    from ..backtest.store_backup import ENV_BUCKET, state_path

    env = os.environ if env is None else env
    target = (env.get(ENV_BUCKET) or "").strip().strip("/")
    if not target:
        return BackupSignals(armed=False, last_success_age_s=None)
    now_ms = now_ms or int(time.time() * 1000)
    age: float | None = None
    with contextlib.suppress(Exception):
        state = json.loads(state_path(store_dir(store_root)).read_text())
        last = datetime.fromisoformat(state["last_success_utc"])
        age = now_ms / 1000 - last.timestamp()
    return BackupSignals(armed=True, last_success_age_s=age, target=target)


def empty_feeds(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    window_ms: int = 7_200_000,     # 2 h of beats must ALL read 0 to flag
    min_beats: int = 3,             # one bad tick is an API hiccup, not an outage
    mode: str | None = None,
) -> dict[str, float]:
    """Feeds the latest tick required that have been empty for the whole window.

    A dead candle feed is invisible to the liveness checks: enrich_view
    degrades every fetch per-coin to "skip", so the loop keeps beating while
    the agents on that feed see no bars and hold forever — for weeks that
    reads exactly like "no signal" (B-FEEDHB). This inspects the feed-coverage
    JSON heartbeats now carry: a feed key present in the NEWEST beat (i.e.
    still required by the current roster) whose coin count is 0 across every
    beat in the window (≥ ``min_beats`` observations) is returned, mapped to
    hours since the oldest such observation. Legacy NULL/unparseable rows are
    ignored; a key the roster dropped disappears from new beats and stops
    being judged. Read-only and exception-safe: any failure returns ``{}``
    (the freshness checks own "no heartbeats at all").
    """
    try:
        sql = "SELECT ts_ms, feeds FROM tick_heartbeats WHERE ts_ms >= ?"
        args: list[object] = [now_ms - window_ms]
        if mode is not None:
            sql += " AND mode = ?"
            args.append(mode)
        rows = conn.execute(sql + " ORDER BY ts_ms DESC", args).fetchall()
    except sqlite3.OperationalError:   # pre-migration DB: no feeds column
        return {}
    parsed: list[tuple[int, dict]] = []
    for ts, raw in rows:
        if raw is None:
            continue
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict):
            parsed.append((int(ts), d))
    if not parsed:
        return {}
    latest = parsed[0][1]
    out: dict[str, float] = {}
    for key, val in latest.items():
        if val:
            continue
        obs = [(ts, d[key]) for ts, d in parsed if key in d]
        if len(obs) < min_beats or any(v for _, v in obs):
            continue
        out[key] = (now_ms - min(ts for ts, _ in obs)) / 3_600_000
    return out


def resolve_paper_db_path(
    db_path: Path | str, env: Mapping[str, str] | None = None
) -> Path | None:
    """The *separate* paper DB beside ``db_path``, or None.

    One resolution rule for everyone who needs the split paper book
    (B-PAPERLOOP keeps paper evidence in its own DB): HLBOT_PAPER_DB env,
    else ``hlbot_paper.sqlite`` beside the live DB — exactly what
    deploy/run-paper-tick.sh exports. None when the file is missing or
    resolves to ``db_path`` itself (single-DB setup: the main conn already
    holds whatever paper book exists).
    """
    env = os.environ if env is None else env
    paper_path = Path(env.get(PAPER_DB_ENV) or Path(db_path).parent / PAPER_DB_BASENAME)
    try:
        if not paper_path.is_file() or paper_path.resolve() == Path(db_path).resolve():
            return None
    except OSError:
        return None
    return paper_path


def read_paper_signals(
    db_path: Path | str,
    *,
    now_ms: int | None = None,
    env: Mapping[str, str] | None = None,
) -> PaperSignals:
    """Read the paper loop's liveness from its dedicated DB, if one exists.

    The paper DB lives beside the live DB (``data/hlbot_paper.sqlite``,
    overridable via HLBOT_PAPER_DB — same resolution run-paper-tick.sh uses).
    No paper DB ⇒ not a paper box, stay silent; the file pointing back at
    ``db_path`` itself ⇒ no *separate* loop to monitor (the tick check already
    covers that DB). Read-only open; any read failure degrades to "present but
    never beat" — warn-territory, never a crash inside ``hlbot health``.
    """
    now_ms = now_ms or int(time.time() * 1000)
    paper_path = resolve_paper_db_path(db_path, env=env)
    if paper_path is None:
        return PaperSignals(present=False, beat_age_s=None)
    beat_age: float | None = None
    feeds_gap: dict[str, float] = {}
    with contextlib.suppress(Exception):
        pconn = sqlite3.connect(f"file:{paper_path}?mode=ro", uri=True)
        try:
            row = pconn.execute(
                "SELECT MAX(ts_ms) FROM tick_heartbeats WHERE mode = 'paper'"
            ).fetchone()
            if row and row[0] is not None:
                beat_age = (now_ms - int(row[0])) / 1000
            feeds_gap = empty_feeds(pconn, now_ms=now_ms, mode="paper")
        finally:
            pconn.close()
    return PaperSignals(present=True, beat_age_s=beat_age, empty_feeds=feeds_gap)


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
    daily_loss_floor: float = UNARMED_LOSS_FLOOR,  # negative $ floor on BOT pnl
    deploy: DeploySignals | None = None,
    max_update_age_s: int = 7200,     # 2 h ≈ 8 missed 15-min updater fires
    pager: PagerSignals | None = None,
    paper: PaperSignals | None = None,
    max_paper_age_s: int = 3600,      # 1 h ≈ 12 missed 5-min paper fires
    backup: BackupSignals | None = None,
    max_backup_age_s: int = 10_800,   # 3 h ≈ 3 missed ~hourly uploads
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

    # --- feed coverage (is the beating loop actually seeing the market?) ---
    # A dead candle feed never trips the freshness checks: enrich_view degrades
    # per-coin to "skip", the tick completes, the heartbeat lands — and the
    # agents on that feed hold forever, indistinguishable from "no signal"
    # (B-FEEDHB). Warn-only: flags a feed the latest tick still required whose
    # coverage has been 0 coins across every beat in the window.
    if last_beat is not None:
        gaps = empty_feeds(conn, now_ms=now_ms)
        if gaps:
            detail = ", ".join(
                f"{k} empty for {h:.1f} h" for k, h in sorted(gaps.items()))
            checks.append(("feeds", "warn",
                           f"{detail} — agents on it see no bars "
                           "(candle API outage?)"))

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

    # --- paper forward-test loop (are the G1 calendar clocks running?) ---
    # B-PAPERLOOP found the pipeline dead for days with zero signal: no paper
    # ticks anywhere meant every promotion candidate's 30d calendar clock was
    # silently stopped, and nothing watched the watcher. Gated to boxes where
    # a paper DB exists (dev clones stay quiet); warn-only — a stalled paper
    # loop costs evidence-days, not money, and must not page or block ticks.
    if paper is not None and paper.present:
        metrics["paper_beat_age_s"] = paper.beat_age_s
        if paper.beat_age_s is None:
            checks.append(("paper", "warn",
                           "paper DB exists but no paper tick has completed"))
        elif paper.beat_age_s > max_paper_age_s:
            checks.append(("paper", "warn",
                           f"paper loop stale: last paper tick "
                           f"{paper.beat_age_s/3600:.1f} h ago — G1 calendar "
                           f"clocks are gapping"))
        else:
            checks.append(("paper", "ok",
                           f"last paper tick {paper.beat_age_s/60:.1f} min ago"))
        # Same blindness check for the paper loop's own feeds: its beats live
        # in the paper DB, so the main-DB check above can't see them. A blind
        # paper agent accrues "0 trades" instead of G1 evidence for weeks.
        if paper.empty_feeds:
            detail = ", ".join(
                f"{k} empty for {h:.1f} h"
                for k, h in sorted(paper.empty_feeds.items()))
            checks.append(("paper_feeds", "warn",
                           f"{detail} — paper agents on it see no bars"))

    # --- off-host store backup (is armed protection actually happening?) ---
    # backup_store's failures warn in the journal only: with
    # HLBOT_STORE_BACKUP_S3 armed, broken uploads silently lapse the one
    # off-host copy of the irreplaceable 1m sample (B-STOREBKP2). Gated on
    # the env being set so unarmed boxes stay quiet; warn-only — a missed
    # backup costs nothing until the host dies, and must never block ticks.
    if backup is not None and backup.armed:
        metrics["backup_age_s"] = backup.last_success_age_s
        where = f"s3://{backup.target}"
        if backup.last_success_age_s is None:
            checks.append(("backup", "warn",
                           f"armed ({where}) but no upload has ever "
                           "succeeded — the store has no off-host copy"))
        elif backup.last_success_age_s > max_backup_age_s:
            checks.append(("backup", "warn",
                           f"last store upload "
                           f"{backup.last_success_age_s/3600:.1f} h ago "
                           f"({where}) — off-host copy is going stale"))
        else:
            checks.append(("backup", "ok",
                           f"store → {where}, "
                           f"{backup.last_success_age_s/60:.0f} min ago"))

    # --- 24h realized PnL (bot vs whole account — B-PNL-SPLIT) ---
    # The trading address is shared with the operator's manual trading, so the
    # account-wide sum can page the dead-man switch on a manual loss — or mask
    # a real bot bleed under a manual win (seen live: account −$325.80 while
    # the bot's own book was +$0.97; the gap was manual builder-perp fills).
    # The floor judges BOT fills only: `agent` is cloid-resolved at ingest,
    # NULL/'manual' means "not placed by us" ('unknown:' prefixes ARE ours —
    # bot-tagged cloids whose agent name is no longer registered).
    since = now_ms - 86_400_000
    row = conn.execute(
        "SELECT COALESCE(SUM(closed_pnl - fee), 0),"
        "       COALESCE(SUM(CASE WHEN agent IS NOT NULL AND agent <> 'manual'"
        "                    THEN closed_pnl - fee ELSE 0 END), 0)"
        "  FROM fills WHERE time_ms >= ?",
        (since,),
    ).fetchone()
    pnl24_account = float(row[0] or 0.0)
    pnl24 = float(row[1] or 0.0)
    metrics["pnl_24h"] = pnl24                  # bot-only: the floor's subject
    metrics["pnl_24h_account"] = pnl24_account  # whole address, manual included
    detail = f"bot ${pnl24:+.2f}"
    manual = pnl24_account - pnl24
    if abs(manual) >= 0.005:  # only mention manual when it rounds to a cent
        detail += f" (account ${pnl24_account:+.2f}, manual ${manual:+.2f})"
    if pnl24 < daily_loss_floor:
        checks.append(("pnl_24h", "crit", f"{detail} < floor ${daily_loss_floor:+.2f}"))
    else:
        checks.append(("pnl_24h", "ok", detail))

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
