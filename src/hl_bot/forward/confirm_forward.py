"""Nightly forward confirmation loop.

Rebuilds a forward window of `Frame`s from accrued `market_snapshots`, runs each
paper agent through the G0 confirmation harness using its deployed config, and
auto-promotes paper -> live_small only on a passing result with a matching
params_hash and no breached guardrails.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents.base import Agent
from ..backtest.confirm import ConfirmationResult, confirm_strategy
from ..backtest.persist_confirm import save_confirmation_result
from ..cli.factories import AGENT_CLASSES, agent_config, list_confirmable_agents
from ..config import CONFIG_DIR
from ..db.accrue import load_forward_frames
from ..supervisor.goals import evaluate, load_goals
from ..supervisor.loop import _set_mode

log = logging.getLogger(__name__)


@dataclass
class ForwardConfirmOutcome:
    agent: str
    params_hash: str | None
    confirmed: bool
    reasons: list[str]
    promoted: bool = False
    promotion_blocked_reason: str | None = None


def _agent_state(conn: sqlite3.Connection, agent: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT mode, enabled, confirmed_params_hash, confirmed_at_ms, last_confirmed_ms
        FROM agent_state WHERE agent=?
        """,
        (agent,),
    ).fetchone()
    if row is None:
        return {
            "mode": "paper", "enabled": 1,
            "confirmed_params_hash": None, "confirmed_at_ms": None,
            "last_confirmed_ms": None,
        }
    return {
        "mode": row["mode"],
        "enabled": int(row["enabled"]),
        "confirmed_params_hash": row["confirmed_params_hash"],
        "confirmed_at_ms": row["confirmed_at_ms"],
        "last_confirmed_ms": row["last_confirmed_ms"],
    }


def _guardrails_clean(conn: sqlite3.Connection, agent: str) -> tuple[bool, str]:
    """Return (clean, reason) using the agent's YAML goals/guardrails."""
    goals_path = Path(CONFIG_DIR) / f"{agent}.yaml"
    if not goals_path.exists():
        # No YAML means no guardrails; treat as clean.
        return True, ""
    goals = load_goals(goals_path)
    if not goals:
        return True, ""
    for g in goals:
        if g.agent != agent:
            continue
        evals = evaluate(conn, g)
        for e in evals:
            if e.action in ("pause", "demote"):
                return False, f"{e.goal_name}: {e.detail}"
    return True, ""


def _promote_if_safe(
    conn: sqlite3.Connection,
    agent: str,
    result: ConfirmationResult,
    deployed_hash: str,
) -> ForwardConfirmOutcome:
    """Promote paper -> live_small only if all invariants hold."""
    state = _agent_state(conn, agent)
    reasons = list(result.reasons)

    if not result.confirmed:
        return ForwardConfirmOutcome(
            agent=agent, params_hash=result.params_hash,
            confirmed=False, reasons=reasons,
        )

    if result.params_hash != deployed_hash:
        reasons.append("params_hash mismatch against deployed config")
        return ForwardConfirmOutcome(
            agent=agent, params_hash=result.params_hash,
            confirmed=True, reasons=reasons,
            promotion_blocked_reason="params_hash mismatch",
        )

    clean, guard_reason = _guardrails_clean(conn, agent)
    if not clean:
        reasons.append(f"guardrail breach: {guard_reason}")
        return ForwardConfirmOutcome(
            agent=agent, params_hash=result.params_hash,
            confirmed=True, reasons=reasons,
            promotion_blocked_reason=guard_reason,
        )

    if state["mode"] not in ("paper", "live_small"):
        reasons.append(f"mode={state['mode']} cannot be auto-promoted")
        return ForwardConfirmOutcome(
            agent=agent, params_hash=result.params_hash,
            confirmed=True, reasons=reasons,
            promotion_blocked_reason=f"mode={state['mode']}",
        )

    if state["mode"] == "paper":
        ts = int(time.time() * 1000)
        _set_mode(conn, agent, "live_small", reason="forward G0 confirmation passed")
        conn.execute(
            """
            UPDATE agent_state SET
                confirmed_params_hash=?,
                confirmed_at_ms=COALESCE(confirmed_at_ms, ?),
                last_confirmed_ms=?
            WHERE agent=?
            """,
            (deployed_hash, ts, ts, agent),
        )
        log.info("promoted %s to live_small (params_hash=%s)", agent, deployed_hash)
        return ForwardConfirmOutcome(
            agent=agent, params_hash=deployed_hash,
            confirmed=True, reasons=reasons, promoted=True,
        )

    # live_small already: just refresh confirmation stamp.
    ts = int(time.time() * 1000)
    conn.execute(
        "UPDATE agent_state SET last_confirmed_ms=? WHERE agent=?",
        (ts, agent),
    )
    return ForwardConfirmOutcome(
        agent=agent, params_hash=deployed_hash,
        confirmed=True, reasons=reasons, promoted=False,
    )


def confirm_forward_for_agent(
    conn: sqlite3.Connection,
    agent: str,
    *,
    window_days: int = 30,
    min_is_trades: int = 30,
    min_oos_trades: int = 10,
    prefer: str = "maker",
) -> ForwardConfirmOutcome:
    """Run forward G0 confirmation for a single agent and handle promotion."""
    cfg, deployed_hash = agent_config(agent)
    state = _agent_state(conn, agent)

    end_ms = int(time.time() * 1000)
    start_ms = state["last_confirmed_ms"]
    if start_ms is None:
        start_ms = end_ms - window_days * 86_400_000

    frames = load_forward_frames(conn, start_ms=start_ms, end_ms=end_ms)
    if len(frames) < 10:
        return ForwardConfirmOutcome(
            agent=agent, params_hash=deployed_hash,
            confirmed=False,
            reasons=[f"only {len(frames)} forward frames; need >=10"],
        )

    # Persist the deployed config in the outer DB, then run confirmation with a
    # fresh agent wired to the backtester's in-memory DB so position tracking
    # works correctly.
    AGENT_CLASSES[agent](config=cfg, conn=conn)

    def _factory(bt_conn: sqlite3.Connection) -> Agent:
        return AGENT_CLASSES[agent](config=cfg, conn=bt_conn)

    result = confirm_strategy(
        _factory,
        frames,
        prefer=prefer,
        min_is_trades=min_is_trades,
        min_oos_trades=min_oos_trades,
        params_hash=deployed_hash,
    )
    window_start_ms = frames[0].ts_ms
    window_end_ms = frames[-1].ts_ms
    save_confirmation_result(conn, result, window_start_ms=window_start_ms, window_end_ms=window_end_ms)

    return _promote_if_safe(conn, agent, result, deployed_hash)


def run_forward_confirmation(
    conn: sqlite3.Connection,
    *,
    window_days: int = 30,
    min_is_trades: int = 30,
    min_oos_trades: int = 10,
    prefer: str = "maker",
    agents: list[str] | None = None,
) -> list[ForwardConfirmOutcome]:
    """Run forward G0 confirmation for all (or specified) paper agents."""
    targets = agents or list_confirmable_agents()
    outcomes: list[ForwardConfirmOutcome] = []
    for agent in targets:
        try:
            outcome = confirm_forward_for_agent(
                conn, agent,
                window_days=window_days,
                min_is_trades=min_is_trades,
                min_oos_trades=min_oos_trades,
                prefer=prefer,
            )
            outcomes.append(outcome)
        except Exception as e:  # noqa: BLE001
            log.exception("forward confirm failed for %s", agent)
            outcomes.append(ForwardConfirmOutcome(
                agent=agent, params_hash=None, confirmed=False,
                reasons=[f"internal error: {e}"],
            ))
    return outcomes
