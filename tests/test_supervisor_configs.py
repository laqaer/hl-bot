"""Integration tests for the supervisor configs added for TWAP and FEMR.

These pin two properties:
  1. The YAMLs load and a bleeding agent trips a pause/demote guardrail.
  2. The promotion gates do NOT fire for a losing agent (no accidental
     promotion to live size).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hl_bot.db.schema import init_db
from hl_bot.supervisor.goals import load_goals
from hl_bot.supervisor.loop import run_once

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "supervisor.sqlite")


def _insert_fill(conn, agent, coin, t_ms, pnl, fee=0.1, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_twap_and_femr_configs_load():
    twap = load_goals(CONFIG_DIR / "twap_mr_v1.yaml")
    femr = load_goals(CONFIG_DIR / "femr_v1.yaml")
    assert twap[0].agent == "twap_mr_v1"
    assert femr[0].agent == "femr_v1"
    # No config promotes straight to full live.
    for goals in (twap, femr):
        assert goals[0].promotion is not None
        assert goals[0].promotion.to_mode == "live_small"


def test_trend_breakout_config_loads_and_is_paper_only():
    """The first G0-confirmed strategy ships a paper config that never auto-
    promotes past live_small (live stays a human action)."""
    goals = load_goals(CONFIG_DIR / "trend_breakout_v1.yaml")
    g = goals[0]
    assert g.agent == "trend_breakout_v1"
    assert g.mode == "paper"
    assert g.promotion is not None
    assert g.promotion.to_mode == "live_small"
    # G1 paper gate: promotion requires >=150 trades on a real sample.
    n_trades_conds = [
        c for c in g.promotion.conditions if c.metric == "n_trades"
    ]
    assert n_trades_conds and n_trades_conds[0].threshold >= 150


def test_bleeding_twap_is_paused_by_supervisor(conn):
    now = int(time.time() * 1000)
    # Simulate a previously promoted agent: pause must force it back to paper,
    # not leave a disabled live_small state behind.
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('twap_mr_v1', 'live_small', 1)"
    )
    # $60 of realized loss in the last 24h -> beyond the -$30 pause guardrail.
    for i in range(6):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000, pnl=-10.0)

    goals = load_goals(CONFIG_DIR / "twap_mr_v1.yaml")
    actions = run_once(conn, goals)

    assert "twap_mr_v1" in actions
    assert any("PAUSE" in a for a in actions["twap_mr_v1"])

    state = conn.execute(
        "SELECT enabled, mode FROM agent_state WHERE agent='twap_mr_v1'"
    ).fetchone()
    assert state is not None
    assert state["enabled"] == 0
    assert state["mode"] == "paper"


def test_losing_agent_is_not_promoted(conn):
    now = int(time.time() * 1000)
    # Plenty of trades but negative pnl -> promotion gates must fail.
    for i in range(300):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000, pnl=-0.20)

    goals = load_goals(CONFIG_DIR / "twap_mr_v1.yaml")
    actions = run_once(conn, goals)

    promoted = any("PROMOTE" in a for a in actions.get("twap_mr_v1", []))
    assert promoted is False


def test_guardrail_failure_blocks_promotion_even_if_longer_window_passes(conn):
    """A failed guardrail dominates promotion gates.

    Regression: TWAP was paused for 24h loss and promoted to live_small in the
    same supervisor run because promotion gates were evaluated independently.
    """
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('twap_mr_v1', 'live_small', 1)"
    )
    # Old profitable history satisfies 30d promotion gates.
    for i in range(220):
        _insert_fill(
            conn,
            "twap_mr_v1",
            "ZEC",
            now - 2 * 86_400_000 - (i + 1) * 1000,
            pnl=0.60,
            fee=0.01,
        )
    # Fresh 24h loss breaches the pause guardrail.
    for i in range(4):
        _insert_fill(conn, "twap_mr_v1", "ADA", now - (i + 1) * 1000, pnl=-10.0)

    goals = load_goals(CONFIG_DIR / "twap_mr_v1.yaml")
    actions = run_once(conn, goals)

    twap_actions = actions.get("twap_mr_v1", [])
    assert any("PAUSE" in a for a in twap_actions)
    assert not any("PROMOTE" in a for a in twap_actions)

    state = conn.execute(
        "SELECT enabled, mode FROM agent_state WHERE agent='twap_mr_v1'"
    ).fetchone()
    assert state is not None
    assert state["enabled"] == 0
    assert state["mode"] == "paper"
