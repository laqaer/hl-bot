"""Roadmap gate readout (G1–G3): where each agent stands on the evidence ladder.

ROADMAP_TO_1M.md §4 deploys capital only as gates pass:

  G0 Sim          — positive net-of-cost edge on ≥90d history, walk-forward +
                    cost stress. Evaluated by ``hlbot confirm`` (ad-hoc backtest
                    runs are not recorded in the DB, so G0 is out of scope here).
  G1 Paper        — ≥30d paper, edge ≥ +5 bps, ≥150 trades, no guardrail breach
                    → unlocks live_small.
  G2 Live-small   — ≥30d live, positive net after real fills/funding, max DD
                    < 10% → unlocks scaling via the 5×/1× rule.
  G3 Track record — ≥60d live, stable Sharpe, controlled DD → core capital /
                    vault (Path A / Path C).

This module turns that table into code: pure evaluators over the existing
scorecards (paper replay / exchange fills), the book spans, and the
``goal_evaluations`` audit trail. The readout is informational and read-only —
it never flips a mode. Promotion stays human-gated (B-PAPER3c); this exists so
"is this agent ready?" has one auditable answer instead of a mental join
across ``score --paper``, ``goal_evaluations``, and ``track-record``.

Where the roadmap is qualitative the bar is pre-declared here, conservatively:

- *Evidence duration* is the span from first to last paper exec row / live
  fill. The YAML promotion gates use a 30d scorecard *window*, which a hot
  5-day book can pass on recency alone; the span check closes that hole, and a
  book whose loop died weeks ago does not age into a pass either.
- *No guardrail breach* (G1) counts every failed guardrail evaluation in the
  audit trail over the last 30d — pause, demote, and alert alike.
- *Stable Sharpe* (G3) = Sharpe ≥ 1.0 over the full live span AND ≥ 0.0 over
  the most recent 30d (the recent regime hasn't flipped the strategy).
- *Controlled DD* needs the agent's ``capital:`` base from its goals YAML;
  without one ``max_drawdown`` is N/A and the check reports unknown — an
  unknown never passes a gate.
- ``n_trades`` counts fill legs (entry + exit each), matching the scorecard
  metric the YAML promotion conditions already use.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..scoring.metrics import score_agent
from ..scoring.paper import score_paper_agent

DAY_MS = 86_400_000

# Pre-declared bars (ROADMAP_TO_1M.md §4). Tightening these is fine; loosening
# them is a strategy-promotion change and belongs to the operator.
G1_MIN_DAYS = 30.0
G1_MIN_EDGE_BPS = 5.0
G1_MIN_TRADES = 150
G1_MAX_BREACHES = 0
G2_MIN_DAYS = 30.0
G2_MAX_DD = -0.10
G3_MIN_DAYS = 60.0
G3_MIN_SHARPE_ALL = 1.0
G3_MIN_SHARPE_30D = 0.0
G3_MAX_DD = -0.10

GATE_TITLES = {
    "G1": "G1 Paper",
    "G2": "G2 Live-small",
    "G3": "G3 Track record",
}
GATE_UNLOCKS = {
    "G1": "live_small (human-gated)",
    "G2": "scale via 5x/1x rule",
    "G3": "core capital / vault",
}


@dataclass(frozen=True)
class GateCheck:
    """One evidence criterion. ``passed=None`` means not evaluable from the
    available evidence (missing metric/capital base) — an unknown blocks the
    gate exactly like a fail, it just reads differently."""

    name: str
    passed: bool | None
    value: float | None
    detail: str


@dataclass(frozen=True)
class GateResult:
    gate: Literal["G1", "G2", "G3"]
    agent: str
    source: Literal["paper", "fills"]
    checks: list[GateCheck]

    @property
    def passed(self) -> bool:
        return all(c.passed is True for c in self.checks)

    @property
    def blockers(self) -> list[GateCheck]:
        return [c for c in self.checks if c.passed is not True]


def paper_span_ms(conn: sqlite3.Connection, agent: str) -> tuple[int, int] | None:
    """(first, last) ts of the agent's executable paper rows, or None."""
    row = conn.execute(
        """SELECT MIN(ts_ms), MAX(ts_ms) FROM agent_decisions
           WHERE agent=? AND is_paper=1 AND coin IS NOT NULL
             AND action IN ('place','flatten')""",
        (agent,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def fills_span_ms(conn: sqlite3.Connection, agent: str) -> tuple[int, int] | None:
    """(first, last) ts of the agent's exchange fills, or None."""
    row = conn.execute(
        "SELECT MIN(time_ms), MAX(time_ms) FROM fills WHERE agent=?", (agent,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])


def guardrail_breaches(conn: sqlite3.Connection, agent: str, since_ms: int) -> int:
    """Failed guardrail evaluations in the audit trail since ``since_ms``."""
    row = conn.execute(
        """SELECT COUNT(*) FROM goal_evaluations
           WHERE agent=? AND goal_name LIKE 'guardrail:%'
             AND status='fail' AND ts_ms >= ?""",
        (agent, since_ms),
    ).fetchone()
    return int(row[0])


def _span_check(name: str, span: tuple[int, int], min_days: float, kind: str) -> GateCheck:
    days = (span[1] - span[0]) / DAY_MS
    return GateCheck(
        name=name, passed=days >= min_days, value=days,
        detail=f"{kind} evidence spans {days:.1f}d (need >= {min_days:.0f}d)",
    )


def evaluate_g1(
    conn: sqlite3.Connection,
    agent: str,
    now_ms: int | None = None,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
    breach_conns: list[sqlite3.Connection] | None = None,
) -> GateResult | None:
    """G1 Paper: ≥30d paper, edge ≥ +5bps, ≥150 trades, no guardrail breach.

    Returns None when the agent has no paper book (gate not applicable).
    The 30d paper scorecard uses the supervisor's cost semantics (modeled
    taker costs; modeled funding when ``funding_by_coin`` is supplied — do not
    judge a funding strategy on a funding=0 card). ``breach_conns`` lets a
    split-DB deployment count guardrail breaches from BOTH audit trails (the
    paper supervisor writes to the paper DB, live demotions to the live DB);
    default = ``conn`` only.
    """
    span = paper_span_ms(conn, agent)
    if span is None:
        return None
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    card = score_paper_agent(
        conn, agent, "30d", now_ms=now_ms, funding_by_coin=funding_by_coin)
    breaches = sum(
        guardrail_breaches(c, agent, since_ms=now_ms - 30 * DAY_MS)
        for c in (breach_conns or [conn])
    )
    if card.edge_bps is None:
        edge = GateCheck("edge_bps_30d", None, None,
                         "edge N/A (no paper fills in the last 30d)")
    else:
        edge = GateCheck(
            "edge_bps_30d", card.edge_bps >= G1_MIN_EDGE_BPS, card.edge_bps,
            f"30d paper edge {card.edge_bps:+.1f}bps (need >= +{G1_MIN_EDGE_BPS:.0f})")
    return GateResult(gate="G1", agent=agent, source="paper", checks=[
        _span_check("paper_span_days", span, G1_MIN_DAYS, "paper"),
        edge,
        GateCheck("n_trades_30d", card.n_trades >= G1_MIN_TRADES, float(card.n_trades),
                  f"30d paper trades {card.n_trades} (need >= {G1_MIN_TRADES})"),
        GateCheck("guardrail_breaches_30d", breaches <= G1_MAX_BREACHES, float(breaches),
                  f"guardrail breaches in 30d: {breaches} (need {G1_MAX_BREACHES})"),
    ])


def _dd_check(dd: float | None, bar: float) -> GateCheck:
    if dd is None:
        return GateCheck("max_drawdown", None, None,
                         "maxDD unknown (set `capital:` in the goals YAML, "
                         "needs >= 3 days of PnL)")
    return GateCheck("max_drawdown", dd > bar, dd,
                     f"maxDD {dd * 100:+.1f}% (need better than {bar * 100:.0f}%)")


def evaluate_g2(
    conn: sqlite3.Connection, agent: str, capital: float | None = None
) -> GateResult | None:
    """G2 Live-small: ≥30d live fills, positive net after fills+funding,
    max DD < 10%. Returns None when the agent has no exchange fills."""
    span = fills_span_ms(conn, agent)
    if span is None:
        return None
    card = score_agent(conn, agent, "all", capital_base=capital)
    return GateResult(gate="G2", agent=agent, source="fills", checks=[
        _span_check("live_span_days", span, G2_MIN_DAYS, "live"),
        GateCheck("net_pnl_all", card.net_pnl > 0, card.net_pnl,
                  f"live net (fills+funding-fees) ${card.net_pnl:+.2f} (need > 0)"),
        _dd_check(card.max_drawdown, G2_MAX_DD),
    ])


def evaluate_g3(
    conn: sqlite3.Connection, agent: str, capital: float | None = None
) -> GateResult | None:
    """G3 Track record: ≥60d live, stable Sharpe (≥1.0 full-span and ≥0.0 over
    the last 30d), max DD < 10%. Returns None when the agent has no fills."""
    span = fills_span_ms(conn, agent)
    if span is None:
        return None
    card_all = score_agent(conn, agent, "all", capital_base=capital)
    card_30 = score_agent(conn, agent, "30d", capital_base=capital)

    def _sharpe_check(name: str, v: float | None, bar: float, label: str) -> GateCheck:
        if v is None:
            return GateCheck(name, None, None, f"{label} sharpe N/A (< 3 PnL days)")
        return GateCheck(name, v >= bar, v,
                         f"{label} sharpe {v:+.2f} (need >= {bar:+.1f})")

    return GateResult(gate="G3", agent=agent, source="fills", checks=[
        _span_check("live_span_days", span, G3_MIN_DAYS, "live"),
        _sharpe_check("sharpe_all", card_all.sharpe, G3_MIN_SHARPE_ALL, "full-span"),
        _sharpe_check("sharpe_30d", card_30.sharpe, G3_MIN_SHARPE_30D, "30d"),
        _dd_check(card_all.max_drawdown, G3_MAX_DD),
    ])


def evaluate_roadmap_gates(
    conn: sqlite3.Connection,
    agent: str,
    capital: float | None = None,
    now_ms: int | None = None,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
    paper_conn: sqlite3.Connection | None = None,
) -> list[GateResult]:
    """Every gate the agent has evidence for: G1 needs a paper book, G2/G3
    need exchange fills. An agent with both books gets all three — the readout
    reports evidence, it doesn't police which ladder rung the agent 'should'
    be on.

    ``paper_conn`` is where the paper book lives when the deployment splits
    evidence across two DBs (B-PAPERLOOP's run-paper-tick.sh writes paper
    decisions + the paper audit trail to ``data/hlbot_paper.sqlite`` while
    fills/agent_state stay in the live DB). Default None = single DB,
    behavior unchanged. G1 breach history is counted from both trails."""
    pconn = paper_conn if paper_conn is not None else conn
    results = [
        evaluate_g1(pconn, agent, now_ms=now_ms, funding_by_coin=funding_by_coin,
                    breach_conns=[pconn] if pconn is conn else [pconn, conn]),
        evaluate_g2(conn, agent, capital=capital),
        evaluate_g3(conn, agent, capital=capital),
    ]
    return [r for r in results if r is not None]


def effective_mode(conn: sqlite3.Connection, agent: str, default: str = "paper") -> str:
    """Current mode: agent_state (supervisor/operator actions) wins over the
    YAML's declared initial mode — same rule as the goals evaluator."""
    row = conn.execute(
        "SELECT mode FROM agent_state WHERE agent=?", (agent,)
    ).fetchone()
    return row["mode"] if row else default
