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
    conditions:
      - {metric: sharpe,   window: 30d, op: ">=", threshold: 2.0}
      - {metric: n_trades, window: 30d, op: ">=", threshold: 100}

Operators: '>', '>=', '<', '<=', '==', '!='.
Metrics: any field on Scorecard (sharpe, net_pnl, win_rate, max_drawdown, ...).
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


@dataclass
class ConditionProgress:
    metric: str
    window: Window
    op: str
    threshold: float
    value: float | None
    status: Literal["pass", "fail", "na"]


@dataclass
class GateProgress:
    """Per-condition progress toward an agent's promotion gate (e.g. G1).

    Unlike ``evaluate``'s promotion check — which only emits a result when ALL
    conditions pass — this reports every condition's current value vs threshold,
    so the distance to the gate is observable while the paper clock is still
    running. Read-only; computes nothing the supervisor doesn't already compute.
    """

    agent: str
    from_mode: str
    to_mode: str
    conditions: list[ConditionProgress]
    n_met: int
    n_total: int
    ready: bool


def load_goals(config_path: str | Path) -> list[AgentGoals]:
    """Load one or more AgentGoals from a YAML file (single doc or list)."""
    raw = yaml.safe_load(Path(config_path).read_text())
    if isinstance(raw, dict):
        raw = [raw]
    return [AgentGoals.model_validate(d) for d in raw]


def evaluate(conn: sqlite3.Connection, g: AgentGoals) -> list[Evaluation]:
    """Run guardrails + promotion/demotion checks, return Evaluations.

    This function does NOT mutate state; the supervisor does that based on the
    actions returned.
    """
    out: list[Evaluation] = []

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

    cards: dict[Window, Scorecard] = {
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
        ))
    for i, s in enumerate(secondary if isinstance(secondary, list) else []):
        c = Condition.model_validate(s)
        ok, v = c.evaluate(cards[c.window])
        out.append(Evaluation(
            agent=g.agent, goal_name=f"secondary[{i}]",
            metric_value=v, threshold=c.threshold,
            status=_status(ok),
            detail=f"{c.metric}({c.window}) {c.op} {c.threshold}",
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
        ))

    # Promotion: ALL conditions must explicitly pass (na blocks promotion), and
    # no guardrail may be failing. Risk controls dominate growth controls: an
    # agent cannot be paused/demoted/alerting and promoted in the same run.
    if g.promotion and g.mode == g.promotion.from_mode and not guardrail_failed:
        results = [c.evaluate(cards[c.window]) for c in g.promotion.conditions]
        if results and all(ok is True for ok, _ in results):
            out.append(Evaluation(
                agent=g.agent, goal_name="promotion",
                metric_value=None, threshold=None,
                status="pass", action="promote",
                detail=f"{g.promotion.from_mode} -> {g.promotion.to_mode}",
            ))

    return out


def promotion_progress(
    conn: sqlite3.Connection, g: AgentGoals
) -> GateProgress | None:
    """Distance-to-gate report for an agent's promotion conditions.

    Returns None if the agent has no promotion block. Each condition is scored
    with the same ``score_agent``/``Condition.evaluate`` the supervisor uses, so
    ``ready`` here matches what ``evaluate`` would promote on (modulo guardrails,
    which are a separate, dominating check). Pure / read-only.
    """
    if not g.promotion:
        return None
    conds: list[ConditionProgress] = []
    for c in g.promotion.conditions:
        sc = score_agent(conn, g.agent, c.window, capital_base=g.capital)
        ok, v = c.evaluate(sc)
        conds.append(ConditionProgress(
            metric=c.metric, window=c.window, op=c.op,
            threshold=c.threshold, value=v,
            status="na" if ok is None else ("pass" if ok else "fail"),
        ))
    n_met = sum(1 for cp in conds if cp.status == "pass")
    ready = bool(conds) and all(cp.status == "pass" for cp in conds)
    return GateProgress(
        agent=g.agent,
        from_mode=g.promotion.from_mode,
        to_mode=g.promotion.to_mode,
        conditions=conds,
        n_met=n_met,
        n_total=len(conds),
        ready=ready,
    )


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
             e.status, e.action, e.detail),
        )
