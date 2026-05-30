"""Supervisor loop: evaluate goals, apply actions, log audit trail."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

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
        INSERT INTO agent_state(agent, mode, enabled, paused_reason, paused_at_ms)
        VALUES(?, 'paper', 0, ?, ?)
        ON CONFLICT(agent) DO UPDATE SET
            enabled = 0,
            paused_reason = excluded.paused_reason,
            paused_at_ms = excluded.paused_at_ms
        """,
        (agent, reason, ts),
    )


def _demote(conn: sqlite3.Connection, agent: str) -> None:
    """live -> live_small -> paper."""
    row = conn.execute("SELECT mode FROM agent_state WHERE agent=?", (agent,)).fetchone()
    cur = row["mode"] if row else "paper"
    new = {"live": "live_small", "live_small": "paper", "paper": "paper"}[cur]
    _set_mode(conn, agent, new, reason="demoted by supervisor")


def run_once(conn: sqlite3.Connection, configs: list[AgentGoals]) -> dict[str, list[str]]:
    """Evaluate all agent configs once. Returns map of agent -> actions taken."""
    actions_taken: dict[str, list[str]] = {}
    for g in configs:
        evals = evaluate(conn, g)
        persist(conn, evals)
        acts: list[str] = []
        for e in evals:
            if e.action == "pause":
                _pause(conn, g.agent, e.detail)
                acts.append(f"PAUSE: {e.detail}")
            elif e.action == "demote":
                _demote(conn, g.agent)
                acts.append(f"DEMOTE: {e.detail}")
            elif e.action == "promote" and g.promotion:
                _set_mode(conn, g.agent, g.promotion.to_mode,
                          reason=f"promoted via {e.detail}")
                acts.append(f"PROMOTE: {e.detail}")
        if acts:
            actions_taken[g.agent] = acts
            log.info("supervisor actions for %s: %s", g.agent, acts)
    return actions_taken


def supervise(conn: sqlite3.Connection, configs_dir: str | Path) -> dict[str, list[str]]:
    """Load every *.yaml in configs_dir and evaluate."""
    configs: list[AgentGoals] = []
    for p in sorted(Path(configs_dir).glob("*.yaml")):
        configs.extend(load_goals(p))
    return run_once(conn, configs)
