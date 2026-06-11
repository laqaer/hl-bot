"""Auto-promotion ladder: DB-mode staging, min-days, G0 requirement, paper vs
live metric sources, kill suppression — the full walk paper -> live_small ->
live on synthetic evidence."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.ops.kill import clear_kill, trip_kill
from hl_bot.supervisor.goals import AgentGoals, evaluate, g0_confirmed
from hl_bot.supervisor.loop import run_once

NOW = int(time.time() * 1000)
DAY = 86_400_000

LADDER_YAML = {
    "agent": "carry",
    "mode": "paper",
    "promotion_ladder": [
        {
            "from": "paper", "to": "live_small",
            "min_days_in_mode": 10, "require_g0": True,
            "conditions": [
                {"metric": "edge_bps", "window": "30d", "op": ">=", "threshold": 3,
                 "source": "paper"},
                {"metric": "n_trades", "window": "30d", "op": ">=", "threshold": 4,
                 "source": "paper"},
            ],
        },
        {
            "from": "live_small", "to": "live",
            "min_days_in_mode": 21,
            "conditions": [
                {"metric": "edge_bps", "window": "30d", "op": ">=", "threshold": 3},
                {"metric": "n_trades", "window": "30d", "op": ">=", "threshold": 2},
            ],
        },
    ],
}

_SEQ = iter(range(1, 100_000))


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def goals():
    return AgentGoals.model_validate(LADDER_YAML)


def paper_fill(conn, t_ms, pnl=5.0, notional=100.0):
    conn.execute(
        """INSERT INTO paper_fills(time_ms, agent, coin, side, px, sz, closed_pnl, fee)
           VALUES(?, 'carry', 'BTC', 'B', ?, 1.0, ?, 0.01)""",
        (t_ms, notional, pnl),
    )


def live_fill(conn, t_ms, pnl=5.0, notional=100.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
                             closed_pnl, fee, agent, raw_json)
           VALUES(?, ?, ?, 'BTC', 'B', ?, 1.0, ?, 0.01, 'carry', '{}')""",
        (f"h{next(_SEQ)}", next(_SEQ), t_ms, notional, pnl),
    )


def decision(conn, t_ms):
    conn.execute(
        "INSERT INTO agent_decisions(ts_ms, agent, action, is_paper) VALUES(?, 'carry', 'hold', 1)",
        (t_ms,),
    )


def g0_stamp(conn, t_ms=None, confirmed=1):
    conn.execute(
        """INSERT INTO confirmations(agent, ts_ms, dataset, prefer, confirmed, oos_edge_bps)
           VALUES('carry', ?, 'test', 'maker', ?, 6.5)""",
        (t_ms or NOW, confirmed),
    )


def seed_paper_evidence(conn, *, first_seen_days_ago=20):
    decision(conn, NOW - first_seen_days_ago * DAY)   # mode-start proxy
    for i in range(5):
        paper_fill(conn, NOW - (i + 1) * DAY)


def promote_actions(conn, g, **kw):
    return [e for e in evaluate(conn, g, **kw) if e.action == "promote"]


def test_promotes_paper_to_live_small_with_all_evidence(conn):
    seed_paper_evidence(conn)
    g0_stamp(conn)
    acts = promote_actions(conn, goals(), current_mode="paper")
    assert len(acts) == 1
    assert acts[0].to_mode == "live_small"


def test_blocked_without_g0(conn):
    seed_paper_evidence(conn)
    assert promote_actions(conn, goals(), current_mode="paper") == []
    # stale G0 doesn't count either
    g0_stamp(conn, t_ms=NOW - 40 * DAY)
    assert promote_actions(conn, goals(), current_mode="paper") == []
    # failed confirmation doesn't count
    g0_stamp(conn, confirmed=0)
    assert promote_actions(conn, goals(), current_mode="paper") == []


def test_blocked_by_min_days_in_mode(conn):
    seed_paper_evidence(conn, first_seen_days_ago=3)   # only 3d in paper
    g0_stamp(conn)
    assert promote_actions(conn, goals(), current_mode="paper") == []
    # promotion timestamp also gates: promoted 2d ago means 2d in mode
    assert promote_actions(conn, goals(), current_mode="paper",
                           last_promoted_ms=NOW - 2 * DAY) == []


def test_live_stage_requires_real_fills_not_paper(conn):
    seed_paper_evidence(conn)
    g0_stamp(conn)
    # In live_small with rich PAPER history but no real fills: must NOT promote.
    acts = promote_actions(conn, goals(), current_mode="live_small",
                           last_promoted_ms=NOW - 30 * DAY)
    assert acts == []
    # Real fills arrive -> promotes to live.
    live_fill(conn, NOW - 2 * DAY)
    live_fill(conn, NOW - 1 * DAY)
    acts = promote_actions(conn, goals(), current_mode="live_small",
                           last_promoted_ms=NOW - 30 * DAY)
    assert len(acts) == 1 and acts[0].to_mode == "live"


def test_full_ladder_walk_via_run_once(conn, tmp_path):
    seed_paper_evidence(conn)
    g0_stamp(conn)
    g = goals()

    # Stage 1: paper -> live_small
    actions = run_once(conn, [g], data_dir=tmp_path)
    assert any(a.startswith("PROMOTE") and "live_small" in a for a in actions["carry"])
    row = conn.execute("SELECT mode, last_promoted_ms FROM agent_state WHERE agent='carry'").fetchone()
    assert row["mode"] == "live_small"

    # Immediately after: live_small stage blocked by min_days (just promoted).
    live_fill(conn, NOW - 2 * DAY)
    live_fill(conn, NOW - 1 * DAY)
    actions = run_once(conn, [g], data_dir=tmp_path)
    assert not any("-> live" in a and a.startswith("PROMOTE:") and "live_small ->" in a
                   for a in actions.get("carry", []))
    assert conn.execute("SELECT mode FROM agent_state WHERE agent='carry'").fetchone()["mode"] == "live_small"

    # Age the promotion 30 days: now it advances to live.
    conn.execute("UPDATE agent_state SET last_promoted_ms=? WHERE agent='carry'",
                 (NOW - 30 * DAY,))
    actions = run_once(conn, [g], data_dir=tmp_path)
    assert any("live_small -> live" in a for a in actions["carry"])
    assert conn.execute("SELECT mode FROM agent_state WHERE agent='carry'").fetchone()["mode"] == "live"


def test_kill_suppresses_promotion_but_not_demotion(conn, tmp_path):
    seed_paper_evidence(conn)
    g0_stamp(conn)
    trip_kill(tmp_path, "halted", alert=False)
    actions = run_once(conn, [goals()], data_dir=tmp_path)
    assert any("PROMOTE-SUPPRESSED" in a for a in actions.get("carry", []))
    assert conn.execute("SELECT mode FROM agent_state WHERE agent='carry'").fetchone() is None
    clear_kill(tmp_path, alert=False)
    actions = run_once(conn, [goals()], data_dir=tmp_path)
    assert conn.execute("SELECT mode FROM agent_state WHERE agent='carry'").fetchone()["mode"] == "live_small"


def test_g0_confirmed_helper(conn):
    assert g0_confirmed(conn, "carry") is False
    g0_stamp(conn)
    assert g0_confirmed(conn, "carry") is True
    assert g0_confirmed(conn, "other") is False
