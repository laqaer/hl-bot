"""Kill switch semantics: sticky file, supervisor suppression, equity floor.

The kill switch is the single emergency brake for the autonomous book. These
tests pin the contract:
  * tripping is sticky and append-only (first reason never overwritten);
  * the supervisor never PROMOTES while killed, but pause/demote still run;
  * the equity-floor backstop fires only on a real HWM breach.
"""

from __future__ import annotations

import time

from hl_bot.db.schema import init_db
from hl_bot.ops.kill import (
    clear_kill,
    equity_floor_breached,
    kill_active,
    kill_path,
    trip_kill,
)
from hl_bot.supervisor.goals import AgentGoals
from hl_bot.supervisor.loop import run_once

NOW_MS = int(time.time() * 1000)


def test_kill_lifecycle(tmp_path):
    assert kill_active(tmp_path) is None
    trip_kill(tmp_path, "test reason", alert=False)
    reason = kill_active(tmp_path)
    assert reason is not None and "test reason" in reason
    # Sticky + append-only: a second trip keeps the first reason.
    trip_kill(tmp_path, "second cause", alert=False)
    reason = kill_active(tmp_path)
    assert "test reason" in reason and "second cause" in reason
    assert clear_kill(tmp_path, alert=False) is True
    assert kill_active(tmp_path) is None
    assert clear_kill(tmp_path, alert=False) is False


def test_manual_touch_counts_as_kill(tmp_path):
    # A bare `touch data/KILL` over SSH must work even with no reason recorded.
    kill_path(tmp_path).touch()
    assert kill_active(tmp_path) is not None


def _goals_with_promotion(agent: str = "a1") -> AgentGoals:
    return AgentGoals.model_validate({
        "agent": agent,
        "mode": "paper",
        "promotion": {
            "from": "paper",
            "to": "live_small",
            "min_days_in_mode": 0,
            "conditions": [
                {"metric": "n_trades", "window": "30d", "op": ">=", "threshold": 1},
            ],
        },
        "guardrails": [
            {"metric": "net_pnl", "window": "24h", "op": ">=", "threshold": -10,
             "action": "pause", "reason": "24h loss"},
        ],
    })


def _seed_fill(conn, agent: str, closed_pnl: float) -> None:
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
                             closed_pnl, fee, agent, raw_json)
           VALUES(?, ?, ?, 'BTC', 'B', 100, 1, ?, 0.1, ?, '{}')""",
        (f"h{closed_pnl}{agent}", abs(hash((agent, closed_pnl))) % 10**9,
         NOW_MS - 3_600_000, closed_pnl, agent),
    )


def test_supervisor_suppresses_promote_under_kill(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    _seed_fill(conn, "a1", closed_pnl=5.0)

    trip_kill(tmp_path, "halted", alert=False)
    actions = run_once(conn, [_goals_with_promotion("a1")], data_dir=tmp_path)
    assert any("PROMOTE-SUPPRESSED" in a for a in actions.get("a1", []))
    row = conn.execute("SELECT mode FROM agent_state WHERE agent='a1'").fetchone()
    assert row is None  # mode never written

    clear_kill(tmp_path, alert=False)
    actions = run_once(conn, [_goals_with_promotion("a1")], data_dir=tmp_path)
    assert any(a.startswith("PROMOTE:") for a in actions.get("a1", []))
    row = conn.execute("SELECT mode FROM agent_state WHERE agent='a1'").fetchone()
    assert row["mode"] == "live_small"


def test_supervisor_still_pauses_under_kill(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    _seed_fill(conn, "a1", closed_pnl=-50.0)  # breaches the -10 guardrail

    trip_kill(tmp_path, "halted", alert=False)
    actions = run_once(conn, [_goals_with_promotion("a1")], data_dir=tmp_path)
    assert any(a.startswith("PAUSE") for a in actions.get("a1", []))
    row = conn.execute("SELECT enabled FROM agent_state WHERE agent='a1'").fetchone()
    assert int(row["enabled"]) == 0


def _seed_equity(conn, ts_ms: int, value: float) -> None:
    conn.execute(
        """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
               total_ntl_pos, total_raw_usd, withdrawable, raw_json)
           VALUES(?, ?, 0, 0, 0, 0, '{}')""",
        (ts_ms, value),
    )


def test_equity_floor_breached(tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    breached, why = equity_floor_breached(conn, now_ms=NOW_MS)
    assert breached is False and "no equity history" in why

    _seed_equity(conn, NOW_MS - 10 * 86_400_000, 1000.0)  # HWM
    _seed_equity(conn, NOW_MS - 1000, 800.0)              # above 75% floor
    breached, _ = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is False

    _seed_equity(conn, NOW_MS, 700.0)                     # below 750 floor
    breached, why = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is True and "HWM" in why
