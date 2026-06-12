"""Goal evaluation on paper-book scorecards (B-PAPER3c).

Paper-mode agents used to be judged on their fills-based cards, which a paper
book never populates — every guardrail was permanently N/A and the supervisor
was blind to the forward-test evidence it was supposed to police. These tests
pin the new sourcing rules:

  1. A paper-mode agent with a paper book is scored from the paper replay, so
     pause/demote guardrails actually fire on paper losses.
  2. Promotion can NEVER be applied from paper evidence — passing every gate
     yields an informational "promotion-ready (human-gated)" evaluation, and
     run_once refuses a paper-sourced promote even if one were emitted.
  3. The *effective* mode (agent_state) picks the book: an operator-promoted
     agent is judged on exchange fills, not its stale paper book.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hl_bot.db.schema import init_db
from hl_bot.supervisor import loop as sup_loop
from hl_bot.supervisor.goals import AgentGoals, Evaluation, evaluate, load_goals, persist
from hl_bot.supervisor.loop import run_once

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
HOUR_MS = 3_600_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "paper_goals.sqlite")


def _paper(conn, agent, ts_ms, action, coin, side=None, sz=None, px=None):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(?,?,?,?,?,?,?,1)""",
        (ts_ms, agent, action, coin, side, sz, px),
    )


def _insert_fill(conn, agent, coin, t_ms, pnl, fee=0.1, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def test_bleeding_paper_book_trips_breakout_pause_guardrail(conn):
    """The real breakout_v1.yaml pause guardrail (24h net < -$15) fires on a
    paper-book loss — previously impossible (fills card showed 0 trades)."""
    now = int(time.time() * 1000)
    # -$20 round trip well inside 24h: long 2.0 @ 100, flatten @ 90.
    _paper(conn, "breakout_v1", now - 2 * HOUR_MS, "place", "XPL", "B", 2.0, 100.0)
    _paper(conn, "breakout_v1", now - 1 * HOUR_MS, "flatten", "XPL", px=90.0)

    goals = load_goals(CONFIG_DIR / "breakout_v1.yaml")
    actions = run_once(conn, goals)

    assert any("PAUSE" in a for a in actions.get("breakout_v1", []))
    state = conn.execute(
        "SELECT enabled FROM agent_state WHERE agent='breakout_v1'"
    ).fetchone()
    assert state is not None and state["enabled"] == 0
    # The audit trail marks every paper-sourced evaluation.
    details = [r["detail"] for r in conn.execute(
        "SELECT detail FROM goal_evaluations WHERE agent='breakout_v1'")]
    assert details and all(d.startswith("[paper] ") for d in details)


def test_paper_promotion_is_informational_and_never_applied(conn):
    now = int(time.time() * 1000)
    # Two fat winning round trips: gates below pass comfortably net of costs.
    for i, coin in enumerate(("BTC", "ETH")):
        _paper(conn, "papermo", now - (10 - i) * HOUR_MS, "place", coin, "B", 1.0, 100.0)
        _paper(conn, "papermo", now - (8 - i) * HOUR_MS, "flatten", coin, px=120.0)
    g = AgentGoals.model_validate({
        "agent": "papermo",
        "mode": "paper",
        "promotion": {
            "from": "paper", "to": "live_small",
            "conditions": [
                {"metric": "net_pnl", "window": "7d", "op": ">=", "threshold": 5},
                {"metric": "n_trades", "window": "7d", "op": ">=", "threshold": 2},
            ],
        },
    })

    evals = evaluate(conn, g)
    promo = next(e for e in evals if e.goal_name == "promotion")
    assert promo.status == "pass"
    assert promo.action == "none"
    assert promo.source == "paper"
    assert "human-gated" in promo.detail

    actions = run_once(conn, [g])
    assert not any("PROMOTE" in a for a in actions.get("papermo", []))
    state = conn.execute(
        "SELECT mode FROM agent_state WHERE agent='papermo'").fetchone()
    assert state is None or state["mode"] == "paper"


def test_effective_mode_picks_fills_over_stale_paper_book(conn):
    """An operator-promoted agent (agent_state=live_small) is judged on its
    exchange fills; its old bleeding paper book must not pause it."""
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('mixed', 'live_small', 1)"
    )
    _paper(conn, "mixed", now - 3 * HOUR_MS, "place", "XPL", "B", 5.0, 100.0)
    _paper(conn, "mixed", now - 2 * HOUR_MS, "flatten", "XPL", px=50.0)  # -$250 paper
    _insert_fill(conn, "mixed", "BTC", now - HOUR_MS, pnl=2.0)  # healthy live

    g = AgentGoals.model_validate({
        "agent": "mixed",
        "mode": "paper",
        "guardrails": [
            {"metric": "net_pnl", "window": "24h", "op": ">=",
             "threshold": -100, "action": "pause", "reason": "24h loss"},
        ],
    })
    evals = evaluate(conn, g)
    gr = next(e for e in evals if e.goal_name.startswith("guardrail"))
    assert gr.source == "fills"
    assert gr.status == "pass"
    assert gr.metric_value == pytest.approx(1.9)  # 2.0 - 0.1 fee, not -250


def test_promotion_gate_uses_effective_mode_not_yaml(conn):
    """An agent already promoted in agent_state is not re-promoted off a stale
    YAML mode: paper — its fills passing every gate emit no promote action."""
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) VALUES('grown', 'live_small', 1)"
    )
    for i in range(5):
        _insert_fill(conn, "grown", "BTC", now - (i + 1) * 1000, pnl=10.0)
    g = AgentGoals.model_validate({
        "agent": "grown",
        "mode": "paper",  # stale: DB says live_small
        "promotion": {
            "from": "paper", "to": "live_small",
            "conditions": [
                {"metric": "net_pnl", "window": "7d", "op": ">=", "threshold": 5},
            ],
        },
    })
    evals = evaluate(conn, g)
    assert not any(e.goal_name == "promotion" for e in evals)


def test_paper_funding_threads_into_guardrail_metric(conn):
    now = int(time.time() * 1000)
    # Open long 1.0 @ 100 five hours ago, still held.
    _paper(conn, "femr_v1", now - 5 * HOUR_MS, "place", "TRX", "B", 1.0, 100.0)
    g = AgentGoals.model_validate({
        "agent": "femr_v1",
        "mode": "paper",
        "guardrails": [
            {"metric": "net_pnl", "window": "7d", "op": ">=",
             "threshold": -100, "action": "pause", "reason": "7d loss"},
        ],
    })
    # One hourly event at rate -0.1 on a long: -signed×px×rate = +$10.
    funding = {"TRX": [{"time": now - 4 * HOUR_MS, "fundingRate": "-0.1"}]}

    base = next(e for e in evaluate(conn, g)
                if e.goal_name.startswith("guardrail"))
    funded = next(e for e in evaluate(conn, g, paper_funding_by_coin=funding)
                  if e.goal_name.startswith("guardrail"))
    assert funded.metric_value == pytest.approx(base.metric_value + 10.0)


def test_run_once_refuses_paper_sourced_promote(conn, monkeypatch):
    """Defense in depth: even if evaluate() ever emitted a paper-sourced
    promote action, run_once must not apply it."""
    g = AgentGoals.model_validate({
        "agent": "sneaky",
        "mode": "paper",
        "promotion": {"from": "paper", "to": "live_small",
                      "conditions": [{"metric": "n_trades", "window": "7d",
                                      "op": ">=", "threshold": 0}]},
    })
    rogue = Evaluation(
        agent="sneaky", goal_name="promotion", metric_value=None,
        threshold=None, status="pass", action="promote",
        detail="paper -> live_small", source="paper",
    )
    monkeypatch.setattr(sup_loop, "evaluate", lambda *a, **k: [rogue])

    actions = run_once(conn, [g])

    assert not any("PROMOTE" in a for a in actions.get("sneaky", []))
    state = conn.execute(
        "SELECT mode FROM agent_state WHERE agent='sneaky'").fetchone()
    assert state is None


def test_paper_mode_without_paper_book_stays_fills_sourced(conn):
    g = AgentGoals.model_validate({
        "agent": "fresh",
        "mode": "paper",
        "guardrails": [
            {"metric": "net_pnl", "window": "24h", "op": ">=",
             "threshold": -200, "action": "pause", "reason": "24h loss"},
        ],
    })
    evals = evaluate(conn, g)
    assert all(e.source == "fills" for e in evals)
    persist(conn, evals)
    details = [r["detail"] for r in conn.execute(
        "SELECT detail FROM goal_evaluations WHERE agent='fresh'")]
    assert details and not any(d.startswith("[paper]") for d in details)
