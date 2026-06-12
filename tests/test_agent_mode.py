"""Operator mode changes (`hlbot agent-mode` / supervisor.operator).

The documented GO_LIVE procedure for flipping an agent live was raw SQL: no
agent-name validation, no evidence readout, no audit trail, and no unpause
path anywhere in the codebase (`_pause` sets enabled=0; nothing ever set it
back). These tests pin the validated replacement's risk asymmetry:

- tightening always applies; loosening needs explicit confirm;
- loosening moves one rank at a time (paper -> live_small -> live);
- loosening re-checks the supervisor's promotion evidence gates and needs a
  second explicit flag to override them — and the override is recorded;
- --enable clears a recorded pause;
- every applied change writes a goal_evaluations audit row that can never be
  mistaken for a guardrail breach by the clean-guardrails promotion gate.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.supervisor.goals import AgentGoals
from hl_bot.supervisor.loop import _pause
from hl_bot.supervisor.operator import (
    OperatorError,
    apply_mode_change,
    current_state,
    evidence_readout,
    list_states,
    plan_mode_change,
)

DAY_MS = 86_400_000
KNOWN = {"twap_mr_v1", "breakout_er_v1"}


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "agent_mode.sqlite")


def _contract(agent="twap_mr_v1", to="live_small", min_span_days=30, clean_days=30):
    return AgentGoals.model_validate({
        "agent": agent,
        "mode": "paper",
        "promotion": {
            "from": "paper", "to": to,
            "min_span_days": min_span_days,
            "clean_guardrails_days": clean_days,
            "conditions": [
                {"metric": "n_trades", "window": "30d", "op": ">=", "threshold": 1},
            ],
        },
    })


def _paper_rows(conn, agent, span_days):
    now = int(time.time() * 1000)
    for ts in (now - int(span_days * DAY_MS), now):
        conn.execute(
            """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side,
               sz, px, is_paper) VALUES(?,?,'place','BTC','B',1.0,100.0,1)""",
            (ts, agent),
        )


def _plan(conn, agent="twap_mr_v1", contracts=None, **kw):
    return plan_mode_change(
        conn, agent, known_agents=KNOWN,
        contracts=contracts if contracts is not None else [_contract()], **kw)


def _state_row(conn, agent, mode, enabled, paused_reason=None, paused_at_ms=None):
    conn.execute(
        """INSERT INTO agent_state(agent, mode, enabled, paused_reason,
           paused_at_ms) VALUES(?,?,?,?,?)""",
        (agent, mode, enabled, paused_reason, paused_at_ms),
    )


def _audit_rows(conn, agent):
    return conn.execute(
        "SELECT * FROM goal_evaluations WHERE agent=? ORDER BY id", (agent,)
    ).fetchall()


# --- validation ------------------------------------------------------------

def test_unknown_agent_refused(conn):
    with pytest.raises(OperatorError, match="unknown agent 'twap_mr_v2'"):
        _plan(conn, agent="twap_mr_v2", mode="paper", enabled=False)


def test_invalid_mode_refused(conn):
    with pytest.raises(OperatorError, match="invalid mode 'live-small'"):
        _plan(conn, mode="live-small")


def test_noop_refused(conn):
    # Missing row defaults to paper/on (the schema + filter_live_agents default).
    with pytest.raises(OperatorError, match="no change"):
        _plan(conn, mode="paper", enabled=True)


def test_rank_skip_refused_even_with_all_flags(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=40)
    with pytest.raises(OperatorError, match="skips a rank"):
        _plan(conn, mode="live", enabled=True,
              confirm=True, override_evidence=True)


# --- tightening always applies ----------------------------------------------

def test_tightening_applies_without_confirm(conn):
    _state_row(conn, "twap_mr_v1", "live_small", 1)
    change = _plan(conn, mode="paper", enabled=False)
    assert change.direction == "tighten"
    apply_mode_change(conn, change)
    st = current_state(conn, "twap_mr_v1")
    assert (st.mode, st.enabled) == ("paper", 0)


def test_disable_live_agent_is_tightening(conn):
    _state_row(conn, "twap_mr_v1", "live", 1)
    change = _plan(conn, enabled=False)
    assert change.direction == "tighten"


# --- loosening gates ----------------------------------------------------------

def test_loosening_requires_confirm(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=40)
    with pytest.raises(OperatorError, match="re-run with --confirm"):
        _plan(conn, mode="live_small", enabled=True)


def test_loosening_with_failing_evidence_requires_override(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=2)  # thin book
    with pytest.raises(OperatorError, match="evidence span"):
        _plan(conn, mode="live_small", enabled=True, confirm=True)


def test_loosening_without_contract_is_blocked(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=40)
    with pytest.raises(OperatorError, match="no promotion contract"):
        _plan(conn, mode="live_small", enabled=True, contracts=[], confirm=True)


def test_loosening_with_clean_evidence_applies(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=40)
    change = _plan(conn, mode="live_small", enabled=True, confirm=True)
    assert change.direction == "loosen"
    assert change.blockers == [] and not change.overrode_evidence
    apply_mode_change(conn, change)
    st = current_state(conn, "twap_mr_v1")
    assert (st.mode, st.enabled) == ("live_small", 1)
    row = conn.execute(
        "SELECT last_promoted_ms FROM agent_state WHERE agent='twap_mr_v1'"
    ).fetchone()
    assert row["last_promoted_ms"] is not None


def test_override_is_recorded_on_audit_trail(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=2)
    change = _plan(conn, mode="live_small", enabled=True,
                   confirm=True, override_evidence=True)
    assert change.overrode_evidence and change.blockers
    apply_mode_change(conn, change)
    (row,) = _audit_rows(conn, "twap_mr_v1")
    assert row["goal_name"] == "operator"
    assert "OVERRODE evidence gates" in row["detail"]
    assert "evidence span" in row["detail"]


def test_enable_into_live_mode_recheck_gates(conn):
    # Re-enabling a disabled live_small agent makes it live-capable again:
    # that is a loosening move and re-checks entry gates, even with no rank-up.
    _state_row(conn, "twap_mr_v1", "live_small", 0)
    with pytest.raises(OperatorError, match="re-run with --confirm"):
        _plan(conn, enabled=True)


# --- unpause -----------------------------------------------------------------

def test_enable_clears_pause_marker(conn):
    _pause(conn, "twap_mr_v1", "24h loss limit")  # the supervisor's own pause
    st = current_state(conn, "twap_mr_v1")
    assert st.enabled == 0 and st.paused_reason == "24h loss limit"

    change = _plan(conn, enabled=True)  # paper/off -> paper/on: not live-capable
    assert change.direction == "neutral" and change.unpauses
    apply_mode_change(conn, change)
    st = current_state(conn, "twap_mr_v1")
    assert st.enabled == 1
    assert st.paused_reason is None and st.paused_at_ms is None
    (row,) = _audit_rows(conn, "twap_mr_v1")
    assert "cleared pause (24h loss limit)" in row["detail"]


# --- audit semantics -----------------------------------------------------------

def test_audit_row_never_counts_as_guardrail_breach(conn):
    """Operator rows must not match the clean_guardrails_days breach query
    (goal_name LIKE 'guardrail:%' AND status='fail' AND pause/demote)."""
    _state_row(conn, "twap_mr_v1", "live_small", 1)
    apply_mode_change(conn, _plan(conn, mode="paper", enabled=False))
    n = conn.execute(
        """SELECT COUNT(*) AS n FROM goal_evaluations
           WHERE agent='twap_mr_v1' AND goal_name LIKE 'guardrail:%'
             AND status='fail' AND action_taken IN ('pause','demote')"""
    ).fetchone()["n"]
    assert n == 0
    assert evidence_readout(conn, "twap_mr_v1").breaches_30d == 0


# --- readouts ------------------------------------------------------------------

def test_evidence_readout_prefers_paper_book(conn):
    _paper_rows(conn, "twap_mr_v1", span_days=10)
    ev = evidence_readout(conn, "twap_mr_v1")
    assert ev.book == "paper"
    assert ev.span_days == pytest.approx(10, abs=0.1)


def test_list_states_includes_defaults_and_rows(conn):
    _state_row(conn, "legacy_v0", "live_small", 0)
    states = {s.agent: s for s in list_states(conn, KNOWN)}
    assert states["legacy_v0"].mode == "live_small" and states["legacy_v0"].exists
    assert states["twap_mr_v1"].mode == "paper" and not states["twap_mr_v1"].exists
