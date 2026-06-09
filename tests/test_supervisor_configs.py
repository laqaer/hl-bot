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


# max_drawdown/calmar are computed only for the synthetic '_account' agent (they
# need a capital base). For any real agent they are structurally N/A, so a config
# that gates a real agent on them has a dead gate that can never fire (REVIEW C5).
ACCOUNT_ONLY_METRICS = {"max_drawdown", "calmar"}


def _referenced_metrics(g) -> set[str]:
    metrics: set[str] = set()
    primary = g.goals.get("primary")
    if isinstance(primary, dict):
        metrics.add(primary["metric"])
    secondary = g.goals.get("secondary", []) or []
    for s in secondary if isinstance(secondary, list) else []:
        metrics.add(s["metric"])
    for gr in g.guardrails:
        metrics.add(gr.metric)
    for promo in (g.promotion, g.demotion):
        if promo:
            for c in promo.conditions:
                metrics.add(c.metric)
    return metrics


def test_no_config_gates_a_real_agent_on_account_only_metrics():
    """Every config's gates must key on metrics computable for a real agent.

    Regression for the C5 class of bug: funding_arb_v1 had a max_drawdown demote
    guardrail that could never fire because per-agent max_drawdown is always None.
    """
    checked = 0
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        for g in load_goals(path):
            if g.agent == "_account":
                continue
            bad = _referenced_metrics(g) & ACCOUNT_ONLY_METRICS
            assert not bad, f"{path.name} ({g.agent}) gates on account-only {bad}"
            checked += 1
    assert checked >= 5  # all real-agent configs were actually inspected


def test_funding_arb_demote_fires_on_negative_edge(conn):
    """The funding_arb demote guardrail now keys on edge_bps (was max_drawdown,
    which never fired). A bleeding live_small agent must be demoted to paper."""
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) "
        "VALUES('funding_arb_v1', 'live_small', 1)"
    )
    # 20 fills, each $10 notional, net -$0.15 (pnl -0.05 - fee 0.10): edge ~ -150 bps
    # over 7d (well past the -10 bps demote), while 24h net -$3 stays above the
    # -$200 pause limit, so the agent is demoted rather than paused.
    for i in range(20):
        _insert_fill(conn, "funding_arb_v1", "SOL", now - (i + 1) * 1000,
                     pnl=-0.05, fee=0.10)

    goals = load_goals(CONFIG_DIR / "funding_arb_v1.yaml")
    actions = run_once(conn, goals)

    assert any("DEMOTE" in a for a in actions.get("funding_arb_v1", []))
    state = conn.execute(
        "SELECT mode FROM agent_state WHERE agent='funding_arb_v1'"
    ).fetchone()
    assert state["mode"] == "paper"


def test_dollar_drawdown_demote_fires_on_giveback(conn):
    """The new max_drawdown_usd(7d) demote catches a run-up-then-bleed that the
    edge_bps/net_pnl gates miss.

    The agent peaks at +$129 then bleeds back to +$40 net: 7d net AND edge stay
    positive (so the edge_bps demote stays silent) and the 24h window is flat (no
    pause), yet the $89 peak-to-trough give-back exceeds the $75 demote threshold.
    Only the dollar-drawdown guardrail can demote it — its whole reason to exist.
    """
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO agent_state(agent, mode, enabled) "
        "VALUES('twap_mr_regime_v1', 'live_small', 1)"
    )
    six_days = now - 6 * 86_400_000
    thirty_h = now - 30 * 3_600_000  # outside the 24h window, inside 7d
    # Run-up: +$129 net (10 fills, +13 pnl - 0.1 fee each).
    for i in range(10):
        _insert_fill(conn, "twap_mr_regime_v1", "SOL", six_days - (i + 1) * 1000,
                     pnl=13.0, fee=0.1)
    # Bleed-back: -$88.8 net (8 fills, -11 pnl - 0.1 fee each) -> cum +$40.2.
    for i in range(8):
        _insert_fill(conn, "twap_mr_regime_v1", "SOL", thirty_h - (i + 1) * 1000,
                     pnl=-11.0, fee=0.1)

    goals = load_goals(CONFIG_DIR / "twap_mr_regime_v1.yaml")
    actions = run_once(conn, goals)

    acts = actions.get("twap_mr_regime_v1", [])
    # The demote fired, and it came from the give-back guardrail (not edge_bps).
    assert any("DEMOTE" in a and "give-back" in a for a in acts)
    assert not any("edge" in a for a in acts)  # edge gate stayed within limits

    state = conn.execute(
        "SELECT mode FROM agent_state WHERE agent='twap_mr_regime_v1'"
    ).fetchone()
    assert state["mode"] == "paper"


def test_twap_and_femr_configs_load():
    twap = load_goals(CONFIG_DIR / "twap_mr_v1.yaml")
    femr = load_goals(CONFIG_DIR / "femr_v1.yaml")
    assert twap[0].agent == "twap_mr_v1"
    assert femr[0].agent == "femr_v1"
    # No config promotes straight to full live.
    for goals in (twap, femr):
        assert goals[0].promotion is not None
        assert goals[0].promotion.to_mode == "live_small"


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
