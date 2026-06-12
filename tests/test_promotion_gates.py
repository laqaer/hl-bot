"""Promotion evidence gates (B-G1SPAN): span + clean-guardrail history.

Every promotion block keys its conditions on scorecard windows like "30d",
which bound the *lookback*, not the sample — a paper book born five days ago
passes every "30d" condition on five days of evidence. That is exactly the
thin-sample false-positive shape that produced the "+177bps CONFIRMED" carry
print (2 in-sample trades, Iter 47). G1's wording is "≥30d paper ... no
guardrail breach"; these tests pin the two structural gates that enforce it:

  1. ``min_span_days`` — the evidence book (decision log for paper, fills for
     live) must span that much calendar, regardless of what the windowed
     metrics say.
  2. ``clean_guardrails_days`` — no pause/demote guardrail failure on record
     in the lookback. Alert guardrails never block (they fire on any
     materially losing day by design).

Both default to 0 (off) so inline/legacy configs are unaffected.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hl_bot.db.schema import init_db
from hl_bot.supervisor.goals import AgentGoals, evaluate, load_goals
from hl_bot.supervisor.loop import run_once

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "promotion_gates.sqlite")


def _insert_fill(conn, agent, coin, t_ms, pnl, fee=0.1, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def _paper(conn, agent, ts_ms, action, coin=None, side=None, sz=None, px=None):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(?,?,?,?,?,?,?,1)""",
        (ts_ms, agent, action, coin, side, sz, px),
    )


def _breach_row(conn, agent, ts_ms, action_taken="pause"):
    conn.execute(
        """INSERT INTO goal_evaluations(ts_ms, agent, goal_name, metric_value,
           threshold, status, action_taken, detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        (ts_ms, agent, "guardrail:net_pnl", -20.0, -15.0, "fail", action_taken,
         "24h loss"),
    )


def _gated_goals(agent, *, min_span_days=30, clean_days=30, n_trades=2):
    return AgentGoals.model_validate({
        "agent": agent,
        "mode": "paper",
        "promotion": {
            "from": "paper", "to": "live_small",
            "min_span_days": min_span_days,
            "clean_guardrails_days": clean_days,
            "conditions": [
                {"metric": "net_pnl", "window": "30d", "op": ">=", "threshold": 5},
                {"metric": "n_trades", "window": "30d", "op": ">=",
                 "threshold": n_trades},
            ],
        },
    })


def _winning_fills(conn, agent, *, first_ms, last_ms, n=5):
    """n profitable fills spread between first_ms and last_ms inclusive."""
    step = (last_ms - first_ms) // max(n - 1, 1)
    for i in range(n):
        _insert_fill(conn, agent, "BTC", first_ms + i * step, pnl=10.0)


def test_thin_fills_book_blocks_auto_promotion(conn):
    """The critical path: fills-sourced promotion AUTO-APPLIES, so a 2-day-old
    book passing every '30d' condition must be blocked by the span gate."""
    now = int(time.time() * 1000)
    _winning_fills(conn, "thin", first_ms=now - 2 * DAY_MS, last_ms=now - HOUR_MS)

    g = _gated_goals("thin")
    evals = evaluate(conn, g)
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "fail"
    assert promo.action == "none"
    assert "evidence span 2.0d < 30d required" in promo.detail

    actions = run_once(conn, [g])
    assert not any("PROMOTE" in a for a in actions.get("thin", []))
    state = conn.execute(
        "SELECT mode FROM agent_state WHERE agent='thin'").fetchone()
    assert state is None


def test_seasoned_fills_book_promotes(conn):
    now = int(time.time() * 1000)
    _winning_fills(conn, "ripe", first_ms=now - 31 * DAY_MS, last_ms=now - HOUR_MS)

    evals = evaluate(conn, _gated_goals("ripe"))
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "pass"
    assert promo.action == "promote"


def test_thin_paper_book_blocks_promotion_ready(conn):
    """Paper readiness is informational but feeds a human decision — it must
    not light up on a days-old book either."""
    now = int(time.time() * 1000)
    for i, coin in enumerate(("BTC", "ETH")):
        _paper(conn, "pthin", now - (10 - i) * HOUR_MS, "place", coin, "B", 1.0, 100.0)
        _paper(conn, "pthin", now - (8 - i) * HOUR_MS, "flatten", coin, px=120.0)

    evals = evaluate(conn, _gated_goals("pthin"))
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "fail"
    assert promo.source == "paper"
    assert "evidence span" in promo.detail
    assert "human-gated" not in promo.detail


def test_paper_span_counts_hold_rows_as_observation(conn):
    """A logged hold is the agent alive and deciding — the forward-test clock
    runs over the whole decision log, not just the trades."""
    now = int(time.time() * 1000)
    _paper(conn, "pheld", now - 31 * DAY_MS, "hold")
    for i, coin in enumerate(("BTC", "ETH")):
        _paper(conn, "pheld", now - (10 - i) * HOUR_MS, "place", coin, "B", 1.0, 100.0)
        _paper(conn, "pheld", now - (8 - i) * HOUR_MS, "flatten", coin, px=120.0)

    evals = evaluate(conn, _gated_goals("pheld"))
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "pass"
    assert promo.action == "none"  # paper stays human-gated
    assert "human-gated" in promo.detail


def test_recorded_pause_breach_blocks_promotion(conn):
    now = int(time.time() * 1000)
    _winning_fills(conn, "burnt", first_ms=now - 31 * DAY_MS, last_ms=now - HOUR_MS)
    _breach_row(conn, "burnt", now - 5 * DAY_MS, action_taken="pause")

    evals = evaluate(conn, _gated_goals("burnt"))
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "fail"
    assert promo.action == "none"
    assert "guardrail breach" in promo.detail


def test_alert_failures_do_not_block_promotion(conn):
    now = int(time.time() * 1000)
    _winning_fills(conn, "noisy", first_ms=now - 31 * DAY_MS, last_ms=now - HOUR_MS)
    _breach_row(conn, "noisy", now - 5 * DAY_MS, action_taken="alert")

    promo = next(e for e in evaluate(conn, _gated_goals("noisy"))
                 if e.goal_name == "promotion")
    assert promo.status == "pass"
    assert promo.action == "promote"


def test_breach_outside_lookback_is_forgiven(conn):
    now = int(time.time() * 1000)
    _winning_fills(conn, "healed", first_ms=now - 45 * DAY_MS, last_ms=now - HOUR_MS)
    _breach_row(conn, "healed", now - 40 * DAY_MS, action_taken="demote")

    promo = next(e for e in evaluate(conn, _gated_goals("healed"))
                 if e.goal_name == "promotion")
    assert promo.status == "pass"
    assert promo.action == "promote"


def test_other_agents_breaches_do_not_block(conn):
    now = int(time.time() * 1000)
    _winning_fills(conn, "clean", first_ms=now - 31 * DAY_MS, last_ms=now - HOUR_MS)
    _breach_row(conn, "someone_else", now - 5 * DAY_MS, action_taken="pause")

    promo = next(e for e in evaluate(conn, _gated_goals("clean"))
                 if e.goal_name == "promotion")
    assert promo.action == "promote"


def test_gates_default_off_for_legacy_configs(conn):
    """A promotion block without the new fields behaves exactly as before."""
    now = int(time.time() * 1000)
    _winning_fills(conn, "legacy", first_ms=now - 2 * DAY_MS, last_ms=now - HOUR_MS)
    _breach_row(conn, "legacy", now - 5 * DAY_MS, action_taken="pause")

    g = AgentGoals.model_validate({
        "agent": "legacy",
        "mode": "paper",
        "promotion": {
            "from": "paper", "to": "live_small",
            "conditions": [
                {"metric": "net_pnl", "window": "30d", "op": ">=", "threshold": 5},
            ],
        },
    })
    promo = next(e for e in evaluate(conn, g) if e.goal_name == "promotion")
    assert promo.action == "promote"


def test_no_blocked_eval_when_conditions_fail_anyway(conn):
    """The blocked-promotion audit row only appears when the metrics pass —
    it marks 'looks ready but the evidence is thin', not routine failure."""
    now = int(time.time() * 1000)
    _insert_fill(conn, "meh", "BTC", now - HOUR_MS, pnl=-50.0)

    evals = evaluate(conn, _gated_goals("meh"))
    assert not any(e.goal_name == "promotion" for e in evals)


def test_all_repo_configs_carry_g1_evidence_gates():
    """Pin the G1 pre-registration: every promotion block in configs/ requires
    ≥30d evidence span and a 30d clean pause/demote record. Frozen on day 0 of
    the paper books (2026-06-12) — loosening this is an operator decision."""
    checked = []
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        g = load_goals(path)[0]
        if g.promotion is None:
            continue
        assert g.promotion.min_span_days >= 30, path.name
        assert g.promotion.clean_guardrails_days >= 30, path.name
        checked.append(g.agent)
    assert {"breakout_v1", "breakout_er_v1", "xmom_v1", "twap_mr_v1"} <= set(checked)
