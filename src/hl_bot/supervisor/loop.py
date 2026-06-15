"""Supervisor loop: evaluate goals, apply actions, log audit trail."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..ops.kill import kill_active
from .goals import AgentGoals, evaluate, load_goals, persist

log = logging.getLogger(__name__)


def _set_mode(conn: sqlite3.Connection, agent: str, mode: str, reason: str = "") -> None:
    ts = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO agent_state(agent, mode, enabled, last_promoted_ms)
        VALUES(?, ?, 1, ?)
        ON CONFLICT(agent) DO UPDATE SET
            mode = excluded.mode,
            last_promoted_ms = excluded.last_promoted_ms,
            notes = COALESCE(?, agent_state.notes)
        """,
        (agent, mode, ts, reason or None),
    )


def _pause(conn: sqlite3.Connection, agent: str, reason: str) -> None:
    ts = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO agent_state(agent, mode, enabled, paused_reason, paused_at_ms,
                                last_promoted_ms)
        VALUES(?, 'paper', 0, ?, ?, ?)
        ON CONFLICT(agent) DO UPDATE SET
            mode = excluded.mode,
            enabled = 0,
            paused_reason = excluded.paused_reason,
            paused_at_ms = excluded.paused_at_ms,
            last_promoted_ms = excluded.last_promoted_ms
        """,
        (agent, reason, ts, ts),
    )
    # Resetting last_promoted_ms restarts the min_days clock: without it a
    # paused agent's mode flipped back within minutes on stale evidence.


def _demote(conn: sqlite3.Connection, agent: str) -> None:
    """live -> live_small -> paper."""
    row = conn.execute("SELECT mode FROM agent_state WHERE agent=?", (agent,)).fetchone()
    cur = row["mode"] if row else "paper"
    new = {"live": "live_small", "live_small": "paper", "paper": "paper"}[cur]
    _set_mode(conn, agent, new, reason="demoted by supervisor")


def run_once(
    conn: sqlite3.Connection,
    configs: list[AgentGoals],
    *,
    data_dir: str | Path | None = None,
    params_hashes: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Evaluate all agent configs once. Returns map of agent -> actions taken.

    ``params_hashes`` maps agent -> the deployed config's provenance hash; when
    provided, require_g0 stages only clear with a confirmation stamped for those
    exact params (V3). Omitted (tests) -> no hash matching, as before.

    While the kill switch is active, promotions are suppressed; pause/demote
    (risk-reducing actions) are always processed.
    """
    params_hashes = params_hashes or {}
    kill_reason = kill_active(data_dir) if data_dir is not None else None
    state = {
        r["agent"]: r for r in conn.execute(
            "SELECT agent, mode, enabled, last_promoted_ms FROM agent_state"
        ).fetchall()
    }
    actions_taken: dict[str, list[str]] = {}
    for g in configs:
        st = state.get(g.agent)
        evals = evaluate(
            conn, g,
            current_mode=st["mode"] if st else None,
            last_promoted_ms=st["last_promoted_ms"] if st else None,
            params_hash=params_hashes.get(g.agent),
        )
        persist(conn, evals)
        acts: list[str] = []
        for e in evals:
            if e.action == "pause":
                _pause(conn, g.agent, e.detail)
                acts.append(f"PAUSE: {e.detail}")
            elif e.action == "demote":
                _demote(conn, g.agent)
                acts.append(f"DEMOTE: {e.detail}")
            elif e.action == "promote" and e.to_mode:
                if kill_reason:
                    acts.append(f"PROMOTE-SUPPRESSED (kill active: {kill_reason}): {e.detail}")
                    continue
                if st is not None and not int(st["enabled"]):
                    # Pause is sticky: only a human (unpause) re-enables.
                    acts.append(f"PROMOTE-SUPPRESSED (paused): {e.detail}")
                    continue
                _set_mode(conn, g.agent, e.to_mode,
                          reason=f"promoted via {e.detail}")
                if e.to_mode in ("live_small", "live"):
                    # Paper-position hygiene: stale resting paper quotes must
                    # not fill post-promotion and mint phantom positions.
                    conn.execute("DELETE FROM paper_orders WHERE agent = ?",
                                 (g.agent,))
                acts.append(f"PROMOTE: {e.detail}")
        if acts:
            actions_taken[g.agent] = acts
            log.info("supervisor actions for %s: %s", g.agent, acts)
            _alert(f"🤖 supervisor — {g.agent}: " + " | ".join(acts))
    return actions_taken


def _alert(message: str) -> None:
    try:
        from ..exec.orders import telegram_alert
        telegram_alert(message)
    except Exception:  # noqa: BLE001
        log.debug("supervisor alert not sent")


def unpause(conn: sqlite3.Connection, agent: str) -> bool:
    """Human-only re-enable for a paused agent (the supervisor never does
    this). Leaves the agent in paper with a fresh min_days clock — it must
    re-earn its ladder."""
    ts = int(time.time() * 1000)
    cur = conn.execute(
        """UPDATE agent_state SET enabled = 1, paused_reason = NULL,
           last_promoted_ms = ? WHERE agent = ? AND enabled = 0""",
        (ts, agent),
    )
    return cur.rowcount > 0


def supervise(
    conn: sqlite3.Connection,
    configs_dir: str | Path,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, list[str]]:
    """Load every *.yaml in configs_dir and evaluate against the DEPLOYED
    config's params_hash (V3 provenance), so auto-promotion can only fire on a
    G0 confirmation earned for the params actually running."""
    configs: list[AgentGoals] = []
    for p in sorted(Path(configs_dir).glob("*.yaml")):
        configs.extend(load_goals(p))
    return run_once(conn, configs, data_dir=data_dir,
                    params_hashes=deployed_params_hashes(conn, configs_dir))


def deployed_params_hashes(
    conn: sqlite3.Connection, configs_dir: str | Path
) -> dict[str, str]:
    """Map agent -> params_hash of the CURRENTLY DEPLOYED config (factory
    defaults + agent_overrides.json) — the same roster the engine runs, so the
    G0 gate matches live behaviour. Best-effort: any failure yields {} (gate
    falls back to age-only matching rather than blocking everything)."""
    try:
        from ..agents.fingerprint import config_fingerprint
        from ..engine.runner import _load_overrides, build_roster
        roster = build_roster(conn, configs_dir, _load_overrides(configs_dir))
    except Exception:  # noqa: BLE001 - never let provenance break supervision
        log.exception("could not compute deployed params hashes; G0 falls back to age-only")
        return {}
    return {e.agent.name: config_fingerprint(e.agent) for e in roster}
