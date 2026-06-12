"""Roadmap gate readout (B-GATES) — G1/G2/G3 evaluators.

The YAML promotion gates check 30d-*window* scorecards, so a hot 5-day paper
book could pass them on recency alone, and nothing operationalized the
roadmap's G2/G3 live criteria at all. These tests pin the pre-declared bars:
calendar evidence span, edge/sample floors, breach history (G1), net after
real fills+funding and drawdown (G2), and Sharpe stability (G3) — and that an
unknown (N/A metric) blocks a gate instead of passing it.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.supervisor.gates import (
    evaluate_g1,
    evaluate_g2,
    evaluate_g3,
    evaluate_roadmap_gates,
    fills_span_ms,
    guardrail_breaches,
    paper_span_ms,
)

DAY_MS = 86_400_000
NOW_MS = int(time.time() * 1000)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _paper(conn, agent, ts_ms, action, coin, side=None, sz=None, px=None):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(?,?,?,?,?,?,?,1)""",
        (ts_ms, agent, action, coin, side, sz, px),
    )


def _fill(conn, agent, t_ms, pnl, fee=0.0, sz=1.0, px=100.0, coin="BTC"):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def _breach(conn, agent, ts_ms):
    conn.execute(
        """INSERT INTO goal_evaluations(ts_ms, agent, goal_name, metric_value,
           threshold, status, action_taken, detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        (ts_ms, agent, "guardrail:net_pnl", -50.0, -15.0, "fail", "pause", "24h loss"),
    )


def _seed_paper_book(conn, agent, span_days=31, round_trips=80, win_px=110.0):
    """Profitable paper round trips spread evenly over ``span_days``.

    80 round trips = 160 fill legs, so the trailing 30d window still holds
    >= the 150-leg G1 floor when the book spans 31d; entry 100 -> exit
    ``win_px`` is ~1000bps gross per trip, far above modeled taker costs.
    """
    start = NOW_MS - span_days * DAY_MS
    step = (span_days * DAY_MS - 2_000_000) // max(round_trips, 1)
    for i in range(round_trips):
        t = start + i * step
        _paper(conn, agent, t, "place", "BTC", side="B", sz=1.0, px=100.0)
        _paper(conn, agent, t + 600_000, "flatten", "BTC", px=win_px)


# ---------------------------------------------------------------------------
# spans + breach counting
# ---------------------------------------------------------------------------


def test_spans_none_without_evidence(conn):
    assert paper_span_ms(conn, "ghost") is None
    assert fills_span_ms(conn, "ghost") is None


def test_paper_span_ignores_live_rows_and_holds(conn):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(1000, 'a', 'place', 'BTC', 'B', 1.0, 100.0, 0)""")  # live row
    _paper(conn, "a", 5000, "place", "BTC", side="B", sz=1.0, px=100.0)
    _paper(conn, "a", 9000, "flatten", "BTC", px=101.0)
    assert paper_span_ms(conn, "a") == (5000, 9000)


def test_guardrail_breaches_counts_only_failed_guardrails(conn):
    _breach(conn, "a", 1000)
    conn.execute(
        """INSERT INTO goal_evaluations(ts_ms, agent, goal_name, status)
           VALUES(1000, 'a', 'guardrail:edge_bps', 'pass')""")
    conn.execute(
        """INSERT INTO goal_evaluations(ts_ms, agent, goal_name, status)
           VALUES(1000, 'a', 'primary', 'fail')""")  # goal fail, not a guardrail
    assert guardrail_breaches(conn, "a", since_ms=0) == 1
    assert guardrail_breaches(conn, "a", since_ms=2000) == 0


# ---------------------------------------------------------------------------
# G1 — paper gate
# ---------------------------------------------------------------------------


def test_g1_none_without_paper_book(conn):
    assert evaluate_g1(conn, "ghost") is None


def test_g1_passes_on_mature_profitable_paper_book(conn):
    _seed_paper_book(conn, "cand")
    r = evaluate_g1(conn, "cand", now_ms=NOW_MS)
    assert r is not None and r.gate == "G1" and r.source == "paper"
    assert r.passed, [c.detail for c in r.blockers]
    assert r.blockers == []


def test_g1_blocks_short_span_even_with_strong_30d_card(conn):
    # Same sample compressed into 5 days: every window metric passes, the
    # calendar span check is the one that catches it.
    _seed_paper_book(conn, "hot", span_days=5)
    r = evaluate_g1(conn, "hot", now_ms=NOW_MS)
    assert not r.passed
    assert [c.name for c in r.blockers] == ["paper_span_days"]


def test_g1_blocks_thin_sample(conn):
    _seed_paper_book(conn, "thin", round_trips=10)  # 20 legs < 150
    r = evaluate_g1(conn, "thin", now_ms=NOW_MS)
    assert not r.passed
    assert "n_trades_30d" in [c.name for c in r.blockers]


def test_g1_blocks_negative_edge(conn):
    _seed_paper_book(conn, "loser", win_px=90.0)  # every trip loses ~1000bps
    r = evaluate_g1(conn, "loser", now_ms=NOW_MS)
    assert not r.passed
    assert "edge_bps_30d" in [c.name for c in r.blockers]


def test_g1_counts_recent_breaches_only(conn):
    _seed_paper_book(conn, "cand")
    _breach(conn, "cand", NOW_MS - 40 * DAY_MS)  # outside the 30d window
    assert evaluate_g1(conn, "cand", now_ms=NOW_MS).passed
    _breach(conn, "cand", NOW_MS - 2 * DAY_MS)
    r = evaluate_g1(conn, "cand", now_ms=NOW_MS)
    assert not r.passed
    assert [c.name for c in r.blockers] == ["guardrail_breaches_30d"]


# ---------------------------------------------------------------------------
# G2 — live-small gate
# ---------------------------------------------------------------------------


def _seed_live_book(conn, agent, span_days=31, daily_pnl=10.0):
    for d in range(span_days + 1):
        _fill(conn, agent, NOW_MS - (span_days - d) * DAY_MS, daily_pnl)


def test_g2_none_without_fills(conn):
    assert evaluate_g2(conn, "ghost") is None


def test_g2_passes_on_positive_live_book_with_capital(conn):
    _seed_live_book(conn, "tw")
    r = evaluate_g2(conn, "tw", capital=1000.0)
    assert r is not None and r.gate == "G2" and r.source == "fills"
    assert r.passed, [c.detail for c in r.blockers]


def test_g2_unknown_drawdown_blocks_without_capital(conn):
    # An N/A metric must read as unknown-blocked, never as a pass.
    _seed_live_book(conn, "tw")
    r = evaluate_g2(conn, "tw", capital=None)
    assert not r.passed
    dd = next(c for c in r.blockers if c.name == "max_drawdown")
    assert dd.passed is None


def test_g2_blocks_negative_net_and_short_span(conn):
    _seed_live_book(conn, "bleed", span_days=10, daily_pnl=-5.0)
    r = evaluate_g2(conn, "bleed", capital=1000.0)
    names = [c.name for c in r.blockers]
    assert "live_span_days" in names and "net_pnl_all" in names


def test_g2_net_includes_fees(conn):
    # +10/day gross but fees eat it: net must go negative.
    for d in range(32):
        _fill(conn, "fee", NOW_MS - (31 - d) * DAY_MS, 10.0, fee=12.0)
    r = evaluate_g2(conn, "fee", capital=1000.0)
    assert "net_pnl_all" in [c.name for c in r.blockers]


# ---------------------------------------------------------------------------
# G3 — track-record gate
# ---------------------------------------------------------------------------


def test_g3_passes_on_steady_61d_live_book(conn):
    # Steady positive daily PnL with mild variation: huge Sharpe, ~0 drawdown.
    for d in range(62):
        _fill(conn, "tr", NOW_MS - (61 - d) * DAY_MS, 10.0 + (d % 3))
    r = evaluate_g3(conn, "tr", capital=1000.0)
    assert r.passed, [c.detail for c in r.blockers]


def test_g3_blocks_short_span(conn):
    _seed_live_book(conn, "young", span_days=35)
    r = evaluate_g3(conn, "young", capital=1000.0)
    assert "live_span_days" in [c.name for c in r.blockers]


def test_g3_blocks_recent_collapse(conn):
    # 60d of profit, then the last 25d bleed: full-span sharpe survives but
    # the 30d stability check must catch the regime flip.
    for d in range(86):
        pnl = 12.0 + (d % 3) if d < 61 else -10.0 - (d % 3)
        _fill(conn, "flip", NOW_MS - (85 - d) * DAY_MS, pnl)
    r = evaluate_g3(conn, "flip", capital=10_000.0)
    assert not r.passed
    assert "sharpe_30d" in [c.name for c in r.blockers]


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def test_roadmap_gates_by_evidence(conn):
    _seed_paper_book(conn, "paper_only")
    _seed_live_book(conn, "live_only")
    _seed_paper_book(conn, "both")
    _seed_live_book(conn, "both")
    assert [r.gate for r in evaluate_roadmap_gates(conn, "paper_only")] == ["G1"]
    assert [r.gate for r in evaluate_roadmap_gates(conn, "live_only")] == ["G2", "G3"]
    assert [r.gate for r in evaluate_roadmap_gates(conn, "both")] == ["G1", "G2", "G3"]
    assert evaluate_roadmap_gates(conn, "ghost") == []
