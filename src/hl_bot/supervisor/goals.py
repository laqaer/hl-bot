"""Goals & guardrails: YAML schema + evaluator.

Each agent has a contract:

  agent: funding_arb_v1
  description: Collect funding when 1h rate is extreme
  mode: paper                 # paper / live_small / live  (initial)
  goals:
    primary:
      metric: sharpe
      window: 30d
      op: ">="
      threshold: 1.5
    secondary:
      - {metric: net_pnl, window: 30d, op: ">=", threshold: 500}
      - {metric: win_rate, window: 30d, op: ">=", threshold: 0.55}
  guardrails:
    - {metric: net_pnl, window: 24h, op: ">=", threshold: -200,
       action: pause, reason: "24h loss limit"}
    - {metric: max_drawdown, window: 7d, op: ">=", threshold: -0.10,
       action: demote, reason: "7d drawdown > 10%"}
  promotion:
    from: paper
    to: live_small
    min_span_days: 30          # evidence book must span this many days
    clean_guardrails_days: 30  # no pause/demote breach on record in lookback
    conditions:
      - {metric: sharpe,   window: 30d, op: ">=", threshold: 2.0}
      - {metric: n_trades, window: 30d, op: ">=", threshold: 100}

Operators: '>', '>=', '<', '<=', '==', '!='.
Metrics: any field on Scorecard (sharpe, net_pnl, win_rate, max_drawdown, ...).

Evidence source: an agent whose *effective* mode (agent_state row, falling back
to the YAML's declared mode) is ``paper`` and that has a paper decision book is
scored from the paper-book replay (``score_paper_agent`` — modeled costs, and
modeled funding when the caller supplies rate history); everything else is
scored from exchange fills as before. Guardrails (pause/demote/alert) fire on
paper evidence, but promotion NEVER does: paper cards passing every promotion
gate emit an informational "promotion-ready (human-gated)" evaluation with no
action — going live on modeled fills is a human decision (B-PAPER3c).
"""

from __future__ import annotations

import operator as op
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..scoring.metrics import Scorecard, Window, score_agent
from ..scoring.paper import score_paper_agent

OPS = {
    ">": op.gt, ">=": op.ge, "<": op.lt, "<=": op.le,
    "==": op.eq, "!=": op.ne,
}


class Condition(BaseModel):
    metric: str
    window: Window = "30d"
    op: str
    threshold: float

    def evaluate(self, sc: Scorecard) -> tuple[bool | None, float | None]:
        """Returns (passed, value). passed=None means the metric was N/A
        (missing data); callers should treat that as 'skip', not 'fail'."""
        v = getattr(sc, self.metric, None)
        if v is None:
            return None, None
        return OPS[self.op](v, self.threshold), float(v)


class Guardrail(Condition):
    action: Literal["pause", "demote", "alert"] = "pause"
    reason: str = ""


class Promotion(BaseModel):
    from_mode: str = Field(alias="from")
    to_mode: str = Field(alias="to")
    conditions: list[Condition]
    # G1's structural gates (ROADMAP §4). A metric window like "30d" only
    # bounds the *lookback* — every "30d" condition can pass on a book that is
    # five days old. These bound the evidence itself: the book must span
    # min_span_days of calendar, and the agent must have no pause/demote
    # guardrail breach on record in the last clean_guardrails_days. 0 disables.
    min_span_days: float = 0.0
    clean_guardrails_days: float = 0.0

    model_config = {"populate_by_name": True}


class AgentGoals(BaseModel):
    agent: str
    description: str = ""
    mode: Literal["paper", "live_small", "live"] = "paper"
    # Capital base ($) for per-agent fractional metrics (max_drawdown/Calmar).
    # Required for a drawdown guardrail to be evaluable; without it that metric
    # is N/A and the guardrail can never fire.
    capital: float | None = None
    goals: dict[str, Any] = Field(default_factory=dict)
    guardrails: list[Guardrail] = Field(default_factory=list)
    promotion: Promotion | None = None
    demotion: Promotion | None = None


@dataclass
class Evaluation:
    agent: str
    goal_name: str
    metric_value: float | None
    threshold: float | None
    status: Literal["pass", "fail", "na"]
    action: Literal["promote", "demote", "pause", "none"] = "none"
    detail: str = ""
    # Which book produced the scorecard. A paper-sourced evaluation must never
    # carry action="promote" (live promotion on modeled fills is human-gated).
    source: Literal["fills", "paper"] = "fills"


def load_goals(config_path: str | Path) -> list[AgentGoals]:
    """Load one or more AgentGoals from a YAML file (single doc or list)."""
    raw = yaml.safe_load(Path(config_path).read_text())
    if isinstance(raw, dict):
        raw = [raw]
    return [AgentGoals.model_validate(d) for d in raw]


def _effective_mode(conn: sqlite3.Connection, g: AgentGoals) -> str:
    """Current mode: the agent_state row (supervisor actions / operator
    promotions land there) wins over the YAML's declared *initial* mode."""
    row = conn.execute(
        "SELECT mode FROM agent_state WHERE agent=?", (g.agent,)
    ).fetchone()
    return row["mode"] if row else g.mode


def _evidence_span_days(conn: sqlite3.Connection, agent: str, use_paper: bool) -> float:
    """Calendar span of the evidence book behind the scorecards: first→last
    decision row for paper (ANY action — a logged hold is the agent alive and
    observing, exactly what a forward test accrues), first→last exchange fill
    otherwise."""
    if use_paper:
        row = conn.execute(
            "SELECT MIN(ts_ms) AS lo, MAX(ts_ms) AS hi FROM agent_decisions"
            " WHERE agent=? AND is_paper=1",
            (agent,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(time_ms) AS lo, MAX(time_ms) AS hi FROM fills WHERE agent=?",
            (agent,),
        ).fetchone()
    if row is None or row["lo"] is None:
        return 0.0
    return (row["hi"] - row["lo"]) / 86_400_000.0


def _clean_guardrail_blockers(
    conn: sqlite3.Connection, agent: str, promo: Promotion
) -> list[str]:
    """The clean-guardrails arm of the evidence gates, against one audit
    trail. Split out so the operator path can also run it against the OTHER
    evidence DB when paper and live books live in separate databases
    (B-PAPERLOOP) — a breach recorded in either stream must block."""
    if promo.clean_guardrails_days <= 0:
        return []
    cutoff = int(time.time() * 1000 - promo.clean_guardrails_days * 86_400_000)
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM goal_evaluations
           WHERE agent=? AND goal_name LIKE 'guardrail:%' AND status='fail'
             AND action_taken IN ('pause','demote') AND ts_ms >= ?""",
        (agent, cutoff),
    ).fetchone()
    if row is not None and row["n"]:
        return [
            f"{row['n']} pause/demote guardrail breach(es) on record "
            f"in last {promo.clean_guardrails_days:g}d"
        ]
    return []


def _evidence_blockers(
    conn: sqlite3.Connection, agent: str, promo: Promotion, use_paper: bool
) -> list[str]:
    """Promotion evidence gates that metric conditions can't express.

    Only pause/demote breaches count against clean_guardrails_days: alert
    guardrails fire on any materially losing day by design (e.g. 24h edge
    < -60bps), so counting them would block promotion near-permanently for
    any strategy with losing days.
    """
    blockers: list[str] = []
    if promo.min_span_days > 0:
        span = _evidence_span_days(conn, agent, use_paper)
        if span < promo.min_span_days:
            blockers.append(
                f"evidence span {span:.1f}d < {promo.min_span_days:g}d required"
            )
    blockers.extend(_clean_guardrail_blockers(conn, agent, promo))
    return blockers


def _has_paper_book(conn: sqlite3.Connection, agent: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM agent_decisions
           WHERE agent=? AND is_paper=1 AND coin IS NOT NULL
             AND action IN ('place','flatten') LIMIT 1""",
        (agent,),
    ).fetchone()
    return row is not None


def evaluate(
    conn: sqlite3.Connection,
    g: AgentGoals,
    paper_funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
) -> list[Evaluation]:
    """Run guardrails + promotion/demotion checks, return Evaluations.

    This function does NOT mutate state; the supervisor does that based on the
    actions returned.

    A paper-mode agent with a paper book is scored from the paper-book replay
    (pass ``paper_funding_by_coin`` — raw ``fetch_funding_history`` rows, see
    ``paper_funding_spans`` — so funding strategies aren't judged on funding=0
    cards). Paper evidence can pause/demote/alert but never promotes: when
    every promotion gate passes on a paper card, the returned evaluation is
    informational (action="none", "human-gated" in the detail). The promotion
    mode check uses the *effective* mode, so an agent already promoted in
    agent_state is not re-promoted by a stale YAML ``mode:``.
    """
    out: list[Evaluation] = []

    mode = _effective_mode(conn, g)
    use_paper = mode == "paper" and _has_paper_book(conn, g.agent)
    source: Literal["fills", "paper"] = "paper" if use_paper else "fills"

    # Pre-compute scorecards for all referenced windows.
    windows: set[Window] = set()
    for gr in g.guardrails:
        windows.add(gr.window)
    if g.promotion:
        for c in g.promotion.conditions:
            windows.add(c.window)
    if g.demotion:
        for c in g.demotion.conditions:
            windows.add(c.window)
    primary = g.goals.get("primary")
    if isinstance(primary, dict):
        windows.add(primary.get("window", "30d"))
    secondary = g.goals.get("secondary", [])
    for s in secondary if isinstance(secondary, list) else []:
        windows.add(s.get("window", "30d"))

    if use_paper:
        cards: dict[Window, Scorecard] = {
            w: score_paper_agent(conn, g.agent, w, capital_base=g.capital,
                                 funding_by_coin=paper_funding_by_coin)
            for w in windows
        }
    else:
        cards = {
            w: score_agent(conn, g.agent, w, capital_base=g.capital) for w in windows
        }

    # Primary / secondary goals -> informational pass/fail (no action).
    def _status(ok: bool | None) -> str:
        return "na" if ok is None else ("pass" if ok else "fail")

    if isinstance(primary, dict):
        c = Condition.model_validate(primary)
        ok, v = c.evaluate(cards[c.window])
        out.append(Evaluation(
            agent=g.agent, goal_name="primary",
            metric_value=v, threshold=c.threshold,
            status=_status(ok),
            detail=f"{c.metric}({c.window}) {c.op} {c.threshold}",
            source=source,
        ))
    for i, s in enumerate(secondary if isinstance(secondary, list) else []):
        c = Condition.model_validate(s)
        ok, v = c.evaluate(cards[c.window])
        out.append(Evaluation(
            agent=g.agent, goal_name=f"secondary[{i}]",
            metric_value=v, threshold=c.threshold,
            status=_status(ok),
            detail=f"{c.metric}({c.window}) {c.op} {c.threshold}",
            source=source,
        ))

    # Guardrails: a guardrail "passes" when the condition is satisfied (i.e.
    # the agent is *within* limits). Failing triggers the action. N/A (missing
    # metric — e.g. no trades yet) NEVER triggers an action.
    guardrail_failed = False
    for gr in g.guardrails:
        ok, v = gr.evaluate(cards[gr.window])
        status = _status(ok)
        if status == "fail":
            guardrail_failed = True
        out.append(Evaluation(
            agent=g.agent, goal_name=f"guardrail:{gr.metric}",
            metric_value=v, threshold=gr.threshold,
            status=status,
            action=("none" if status != "fail" else gr.action),  # type: ignore[arg-type]
            detail=gr.reason or f"{gr.metric}({gr.window}) {gr.op} {gr.threshold}",
            source=source,
        ))

    # Promotion: ALL conditions must explicitly pass (na blocks promotion), and
    # no guardrail may be failing. Risk controls dominate growth controls: an
    # agent cannot be paused/demoted/alerting and promoted in the same run.
    if g.promotion and mode == g.promotion.from_mode and not guardrail_failed:
        results = [c.evaluate(cards[c.window]) for c in g.promotion.conditions]
        if results and all(ok is True for ok, _ in results):
            blockers = _evidence_blockers(conn, g.agent, g.promotion, use_paper)
            if blockers:
                # Every metric condition passed but the evidence itself is
                # thin or dirty — the exact thin-sample false-positive G1's
                # "≥30d, no breach" wording exists to stop. Record it (this
                # is the state an operator would otherwise mistake for
                # readiness), promote nothing.
                out.append(Evaluation(
                    agent=g.agent, goal_name="promotion",
                    metric_value=None, threshold=None,
                    status="fail", action="none", source=source,
                    detail=(f"promotion blocked {g.promotion.from_mode} -> "
                            f"{g.promotion.to_mode}: " + "; ".join(blockers)),
                ))
            elif use_paper:
                # Paper evidence flags readiness but NEVER flips a mode:
                # run_once auto-applies "promote", and going live on modeled
                # fills would violate the human-gated live rule.
                out.append(Evaluation(
                    agent=g.agent, goal_name="promotion",
                    metric_value=None, threshold=None,
                    status="pass", action="none", source=source,
                    detail=(f"promotion-ready {g.promotion.from_mode} -> "
                            f"{g.promotion.to_mode} on paper evidence — "
                            "human-gated, not applied"),
                ))
            else:
                out.append(Evaluation(
                    agent=g.agent, goal_name="promotion",
                    metric_value=None, threshold=None,
                    status="pass", action="promote", source=source,
                    detail=f"{g.promotion.from_mode} -> {g.promotion.to_mode}",
                ))

    return out


def persist(conn: sqlite3.Connection, evals: list[Evaluation]) -> None:
    ts = int(time.time() * 1000)
    for e in evals:
        conn.execute(
            """
            INSERT INTO goal_evaluations(
                ts_ms, agent, goal_name, metric_value, threshold,
                status, action_taken, detail
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (ts, e.agent, e.goal_name, e.metric_value, e.threshold,
             e.status, e.action,
             f"[paper] {e.detail}" if e.source == "paper" else e.detail),
        )
