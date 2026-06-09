"""Tests for the daily report's gate-progress section.

Pins that the daily digest surfaces distance-to-gate (the trend_breakout G1
paper clock) so promotion progress is observable without running a separate
command — folding `promotion_progress` into the report (Iter 34).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.db.schema import init_db
from hl_bot.reports.daily import build, render_gate_progress
from hl_bot.supervisor.goals import ConditionProgress, GateProgress

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "report.sqlite")


def test_render_gate_progress_empty_is_blank():
    assert render_gate_progress([]) == ""


def test_render_gate_progress_marks_each_condition():
    gp = GateProgress(
        agent="trend_breakout_v1", from_mode="paper", to_mode="live_small",
        conditions=[
            ConditionProgress("edge_bps", "30d", ">=", 5.0, 0.5, "fail"),
            ConditionProgress("n_trades", "30d", ">=", 150.0, 200.0, "pass"),
            ConditionProgress("net_pnl", "30d", ">=", 50.0, None, "na"),
        ],
        n_met=1, n_total=3, ready=False,
    )
    md = render_gate_progress([gp])
    assert "## Gate progress" in md
    assert "paper → live_small (1/3 met)" in md
    # Per-condition status markers + current value rendered.
    assert "✗ `edge_bps(30d) >= 5`" in md
    assert "✓ `n_trades(30d) >= 150`" in md
    assert "N/A `net_pnl(30d) >= 50`" in md
    assert "→ `+0.50`" in md  # edge value
    assert "→ `—`" in md      # na value


def test_render_gate_progress_ready_header():
    gp = GateProgress(
        agent="x", from_mode="paper", to_mode="live_small",
        conditions=[ConditionProgress("n_trades", "30d", ">=", 1.0, 9.0, "pass")],
        n_met=1, n_total=1, ready=True,
    )
    assert "(READY)" in render_gate_progress([gp])


def test_build_includes_gate_progress_for_partial_trend(conn):
    """A real (paper) config + paper decisions: the digest shows the gate section
    scored from the simulated forward-test, with the n_trades condition met but
    the edge condition still short. (trend_breakout_v1 is paper-mode, so the gate
    is measured from simulated fills, not real ones.)"""
    # 80 round-trips -> 160 simulated fills (n_trades >= 150 ✓); each gains only
    # +0.10/100 of price so the maker round-trip edge is ~+4bps (< +5 gate ✗).
    for _ in range(80):
        log_decision(conn, Decision(agent="trend_breakout_v1", action="place",
                                    coin="BTC", side="B", sz=100.0, px=100.0,
                                    is_paper=True))
        log_decision(conn, Decision(agent="trend_breakout_v1", action="flatten",
                                    coin="BTC", side="A", sz=100.0, px=100.10,
                                    is_paper=True))
    md = build(conn, configs=CONFIG_DIR)
    assert "## HL bot daily report" in md  # base section still present
    assert "## Gate progress" in md
    assert "trend_breakout_v1 — paper → live_small" in md
    assert "paper-sim forward-test" in md  # basis disclosed in the header
    assert "✓ `n_trades(30d) >= 150`" in md
    assert "✗ `edge_bps(30d) >= 5`" in md
