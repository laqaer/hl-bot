"""Split-DB paper evidence (B-PAPERDB).

Since B-PAPERLOOP the real paper book lives in a dedicated DB
(data/hlbot_paper.sqlite, written by deploy/run-paper-tick.sh) while
agent_state, fills, and the live audit trail stay in the live DB. Every
paper-evidence reader that opens one connection therefore judged an EMPTY
book on the live box: `hlbot gates` showed "no evidence yet" for every
candidate and `hlbot agent-mode` would refuse a legitimate promotion (or
normalize --override-evidence) at the ~Jul-12 readiness window. These tests
pin the split-aware paths:

- one shared resolver (ops.health.resolve_paper_db_path);
- gates G1 judged from the paper conn, breach history from BOTH trails;
- operator evidence judged from the paper conn, while the applied change
  lands on the main conn — the DB the live tick obeys;
- a pause/demote breach recorded in EITHER trail blocks a loosening flip;
- default (no paper_conn) behavior byte-identical for single-DB setups.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.ops.health import resolve_paper_db_path
from hl_bot.supervisor.gates import evaluate_roadmap_gates
from hl_bot.supervisor.goals import AgentGoals
from hl_bot.supervisor.operator import (
    OperatorError,
    apply_mode_change,
    evidence_readout,
    plan_mode_change,
)

DAY_MS = 86_400_000
NOW_MS = int(time.time() * 1000)
AGENT = "twap_mr_v1"


@pytest.fixture
def live(tmp_path):
    c = init_db(tmp_path / "hlbot.sqlite")
    yield c
    c.close()


@pytest.fixture
def paper(tmp_path):
    c = init_db(tmp_path / "hlbot_paper.sqlite")
    yield c
    c.close()


def _paper_rows(conn, agent, span_days=31, round_trips=80):
    """Profitable paper round trips spread evenly over ``span_days`` (same
    shape as test_gates: 160 legs >= the G1 150 floor, ~1000bps gross)."""
    start = NOW_MS - int(span_days * DAY_MS)
    step = (int(span_days * DAY_MS) - 2_000_000) // max(round_trips, 1)
    for i in range(round_trips):
        t = start + i * step
        conn.execute(
            """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side,
               sz, px, is_paper) VALUES(?,?,'place','BTC','B',1.0,100.0,1)""",
            (t, agent),
        )
        conn.execute(
            """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side,
               sz, px, is_paper) VALUES(?,?,'flatten','BTC',NULL,NULL,110.0,1)""",
            (t + 600_000, agent),
        )
    conn.commit()


def _breach(conn, agent, ts_ms, action="pause"):
    conn.execute(
        """INSERT INTO goal_evaluations(ts_ms, agent, goal_name, metric_value,
           threshold, status, action_taken, detail)
           VALUES(?,?,'guardrail:net_pnl',-50.0,-15.0,'fail',?, '24h loss')""",
        (ts_ms, agent, action),
    )
    conn.commit()


def _contract(agent=AGENT, to="live_small", min_span_days=30, clean_days=30):
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


# --- resolver ---------------------------------------------------------------

def test_resolver_finds_paper_db_beside_live(tmp_path):
    db = tmp_path / "hlbot.sqlite"
    db.touch()
    assert resolve_paper_db_path(db, env={}) is None  # no paper DB yet
    pdb = tmp_path / "hlbot_paper.sqlite"
    pdb.touch()
    assert resolve_paper_db_path(db, env={}) == pdb


def test_resolver_env_override_and_self_reference(tmp_path):
    db = tmp_path / "hlbot.sqlite"
    db.touch()
    other = tmp_path / "elsewhere.sqlite"
    other.touch()
    assert resolve_paper_db_path(db, env={"HLBOT_PAPER_DB": str(other)}) == other
    # Pointing back at the live DB itself = no SEPARATE paper book.
    assert resolve_paper_db_path(db, env={"HLBOT_PAPER_DB": str(db)}) is None
    assert resolve_paper_db_path(
        db, env={"HLBOT_PAPER_DB": str(tmp_path / "missing.sqlite")}) is None


# --- gates ------------------------------------------------------------------

def test_gates_g1_judged_from_paper_conn(live, paper):
    _paper_rows(paper, AGENT, span_days=31)
    # Without paper_conn the live DB has no paper book: G1 not applicable —
    # exactly the hole this change closes on split-DB boxes.
    assert not [r for r in evaluate_roadmap_gates(live, AGENT) if r.gate == "G1"]
    results = evaluate_roadmap_gates(live, AGENT, paper_conn=paper)
    g1 = next(r for r in results if r.gate == "G1")
    span = next(c for c in g1.checks if c.name == "paper_span_days")
    assert span.passed is True and span.value >= 30


def test_gates_g1_counts_breaches_from_both_trails(live, paper):
    _paper_rows(paper, AGENT, span_days=31)
    # The breach lives in the LIVE trail (e.g. a live demotion) — the paper
    # book itself is clean. It must still count against G1.
    _breach(live, AGENT, NOW_MS - 2 * DAY_MS, action="demote")
    g1 = next(r for r in evaluate_roadmap_gates(live, AGENT, paper_conn=paper)
              if r.gate == "G1")
    breaches = next(c for c in g1.checks if c.name == "guardrail_breaches_30d")
    assert breaches.passed is False and breaches.value == 1.0


# --- operator ---------------------------------------------------------------

def test_evidence_readout_judges_paper_book_from_paper_conn(live, paper):
    _paper_rows(paper, AGENT, span_days=10)
    _breach(live, AGENT, NOW_MS - 3 * DAY_MS)
    _breach(paper, AGENT, NOW_MS - 4 * DAY_MS, action="demote")
    ev = evidence_readout(live, AGENT, paper_conn=paper)
    assert ev.book == "paper"
    assert 9.0 < ev.span_days < 11.0
    assert ev.breaches_30d == 2  # both trails, summed
    # Default single-conn behavior unchanged: no paper book in the live DB.
    assert evidence_readout(live, AGENT).book == "fills"


def test_plan_uses_paper_evidence_and_applies_to_live_conn(live, paper):
    _paper_rows(paper, AGENT, span_days=31)
    change = plan_mode_change(
        live, AGENT, known_agents={AGENT}, contracts=[_contract()],
        mode="live_small", confirm=True, paper_conn=paper,
    )
    assert change.blockers == [] and not change.overrode_evidence
    apply_mode_change(live, change)
    row = live.execute(
        "SELECT mode FROM agent_state WHERE agent=?", (AGENT,)).fetchone()
    assert row["mode"] == "live_small"
    # The paper DB holds no state row: the live tick reads the live DB only.
    assert paper.execute(
        "SELECT COUNT(*) AS n FROM agent_state").fetchone()["n"] == 0


def test_plan_without_paper_conn_sees_no_evidence(live, paper):
    # The pre-fix shape on a split-DB box: judged from the live DB alone the
    # 31d paper book is invisible and the flip is evidence-blocked.
    _paper_rows(paper, AGENT, span_days=31)
    with pytest.raises(OperatorError, match="evidence span 0.0d"):
        plan_mode_change(
            live, AGENT, known_agents={AGENT}, contracts=[_contract()],
            mode="live_small", confirm=True,
        )


def test_live_breach_blocks_paper_evidence_promotion(live, paper):
    _paper_rows(paper, AGENT, span_days=31)
    _breach(live, AGENT, NOW_MS - 2 * DAY_MS, action="demote")
    with pytest.raises(OperatorError, match=r"\(live book\)"):
        plan_mode_change(
            live, AGENT, known_agents={AGENT}, contracts=[_contract()],
            mode="live_small", confirm=True, paper_conn=paper,
        )
    change = plan_mode_change(
        live, AGENT, known_agents={AGENT}, contracts=[_contract()],
        mode="live_small", confirm=True, override_evidence=True,
        paper_conn=paper,
    )
    assert change.overrode_evidence
    assert any("(live book)" in b for b in change.blockers)


# --- CLI wiring -------------------------------------------------------------

def test_agent_mode_cli_reads_split_paper_db(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    live_db = tmp_path / "hlbot.sqlite"
    paper_db = tmp_path / "hlbot_paper.sqlite"
    init_db(live_db).close()
    pconn = init_db(paper_db)
    _paper_rows(pconn, AGENT, span_days=31)
    pconn.close()
    monkeypatch.setenv("HLBOT_DB", str(live_db))
    monkeypatch.setenv("HLBOT_PAPER_DB", str(paper_db))
    res = CliRunner().invoke(app, ["agent-mode", AGENT])
    assert res.exit_code == 0, res.output
    assert "paper book spans 30.6d" in res.output
    assert "separate paper DB" in res.output


def test_gates_cli_reads_split_paper_db(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from hl_bot.cli.main import app

    live_db = tmp_path / "hlbot.sqlite"
    paper_db = tmp_path / "hlbot_paper.sqlite"
    init_db(live_db).close()
    pconn = init_db(paper_db)
    _paper_rows(pconn, AGENT, span_days=31)
    pconn.close()
    monkeypatch.setenv("HLBOT_DB", str(live_db))
    monkeypatch.setenv("HLBOT_PAPER_DB", str(paper_db))
    res = CliRunner().invoke(app, ["gates", "--agent", AGENT, "--no-funding"])
    assert res.exit_code == 0, res.output
    assert "G1" in res.output
    assert "no evidence yet" not in res.output
