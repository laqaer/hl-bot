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

from ..scoring.metrics import Scorecard, Source, Window, score_agent

OPS = {
    ">": op.gt, ">=": op.ge, "<": op.lt, "<=": op.le,
    "==": op.eq, "!=": op.ne,
}


class Condition(BaseModel):
    metric: str
    window: Window = "30d"
    op: str
    threshold: float
    # Which fills the metric is computed from: 'live' = real exchange fills,
    # 'paper' = the simulator's fills. Promotion INTO live must gate on real
    # fills; promotion OUT of paper can only ever gate on paper ones.
    source: Source = "live"

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
    # Minimum days the agent must have spent in from_mode before promotion can
    # fire — staggers correlated promotions and rate-limits the ladder.
    min_days_in_mode: float = 7.0
    # Require a fresh PASSING `hlbot confirm --record` row (the G0 gate on real
    # history) before paper performance is allowed to promote into live.
    require_g0: bool = False
    g0_max_age_days: float = 30.0
    # PERSISTENCE: the stage's conditions must have passed on EVERY supervisor
    # evaluation over this many trailing days (with at least persist_evals
    # looks) before promotion fires. Rolling windows re-checked every ~15min
    # are a first-passage problem — one lucky look must not promote.
    persist_days: float = 3.0
    persist_evals: int = 12

    model_config = {"populate_by_name": True}


class Sizing(BaseModel):
    """Mode-based size caps: live_small runs deliberately tiny regardless of
    what the allocator would grant; full 'live' uses the resolved caps."""
    live_small_max_total: float | None = None
    live_small_max_per_trade: float | None = None
    live_small_fraction: float | None = None    # of the resolved cap


class AgentGoals(BaseModel):
    agent: str
    description: str = ""
    mode: Literal["paper", "live_small", "live"] = "paper"
    # Roster membership: 'live' = eligible for the tick roster, 'paper' = runs
    # paper-only regardless of agent_state, 'retired' = excluded entirely (a
    # retired agent stops consuming MetaAllocator weight).
    roster: Literal["live", "paper", "retired"] = "live"
    # Per-agent re-entry cooldown per coin (replaces the old global 1h constant;
    # carry can stay patient while event-driven agents need minutes or less).
    cooldown_s: int = 3600
    goals: dict[str, Any] = Field(default_factory=dict)
    guardrails: list[Guardrail] = Field(default_factory=list)
    promotion: Promotion | None = None
    # Multi-stage ladder (paper -> live_small -> live). A single `promotion:`
    # block is treated as a one-stage ladder; if both are present the ladder
    # wins for stages it covers.
    promotion_ladder: list[Promotion] = Field(default_factory=list)
    demotion: Promotion | None = None
    sizing: Sizing = Field(default_factory=Sizing)

    def ladder(self) -> list[Promotion]:
        stages = list(self.promotion_ladder)
        if self.promotion and not any(
            p.from_mode == self.promotion.from_mode for p in stages
        ):
            stages.insert(0, self.promotion)
        return stages


@dataclass
class Evaluation:
    agent: str
    goal_name: str
    metric_value: float | None
    threshold: float | None
    status: Literal["pass", "fail", "na"]
    action: Literal["promote", "demote", "pause", "none"] = "none"
    detail: str = ""
    to_mode: str | None = None      # set on promote actions (ladder target)


def load_goals(config_path: str | Path) -> list[AgentGoals]:
    """Load one or more AgentGoals from a YAML file (single doc or list)."""
    raw = yaml.safe_load(Path(config_path).read_text())
    if isinstance(raw, dict):
        raw = [raw]
    return [AgentGoals.model_validate(d) for d in raw]


def g0_confirmed(
    conn: sqlite3.Connection, agent: str, *, max_age_days: float = 30.0,
    now_ms: int | None = None, params_hash: str | None = None,
) -> bool:
    """True when a fresh PASSING `hlbot confirm --record` row exists (G0).

    When ``params_hash`` is given (V3), the confirmation must ALSO have been
    stamped for that exact deployed config — a G0 earned for different params
    no longer counts. Legacy rows (NULL params_hash, pre-V3) never match a
    specific hash, so a config that has never been confirmed under provenance
    must earn a fresh stamp before it can promote.
    """
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - int(max_age_days * 86_400_000)
    sql = "SELECT 1 FROM confirmations WHERE agent=? AND confirmed=1 AND ts_ms>=?"
    args: list = [agent, since]
    if params_hash is not None:
        sql += " AND params_hash=?"
        args.append(params_hash)
    sql += " LIMIT 1"
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _mode_start_ms(
    conn: sqlite3.Connection, agent: str, last_promoted_ms: int | None
) -> int | None:
    """When the agent entered its current mode: last promotion timestamp, or —
    for never-promoted agents — its first recorded decision."""
    if last_promoted_ms:
        return int(last_promoted_ms)
    row = conn.execute(
        "SELECT MIN(ts_ms) FROM agent_decisions WHERE agent=?", (agent,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def evaluate(
    conn: sqlite3.Connection,
    g: AgentGoals,
    *,
    current_mode: str | None = None,
    last_promoted_ms: int | None = None,
    now_ms: int | None = None,
    params_hash: str | None = None,
) -> list[Evaluation]:
    """Run guardrails + promotion/demotion checks, return Evaluations.

    ``current_mode`` is the DB truth from agent_state (the YAML ``mode`` is only
    the agent's *initial* mode — gating on it froze every ladder at stage one).
    ``params_hash`` is the deployed config's provenance hash (V3): when set, a
    require_g0 stage only clears if a confirmation exists for THESE params.
    This function does NOT mutate state; the supervisor does that based on the
    actions returned.
    """
    now_ms = now_ms or int(time.time() * 1000)
    mode = current_mode or g.mode
    out: list[Evaluation] = []

    # Pre-compute scorecards for all referenced (window, source) pairs.
    keys: set[tuple[Window, Source]] = set()
    for gr in g.guardrails:
        keys.add((gr.window, gr.source))
    for p in g.ladder():
        for c in p.conditions:
            keys.add((c.window, c.source))
    if g.demotion:
        for c in g.demotion.conditions:
            keys.add((c.window, c.source))
    primary = g.goals.get("primary")
    if isinstance(primary, dict):
        keys.add((primary.get("window", "30d"), primary.get("source", "live")))
    secondary = g.goals.get("secondary", [])
    for s in secondary if isinstance(secondary, list) else []:
        keys.add((s.get("window", "30d"), s.get("source", "live")))

    cards: dict[tuple[Window, Source], Scorecard] = {
        (w, src): score_agent(conn, g.agent, w, src) for w, src in keys
    }

    # Primary / secondary goals -> informational pass/fail (no action).
    def _status(ok: bool | None) -> str:
        return "na" if ok is None else ("pass" if ok else "fail")

    if isinstance(primary, dict):
        c = Condition.model_validate(primary)
        ok, v = c.evaluate(cards[(c.window, c.source)])
        out.append(Evaluation(
            agent=g.agent, goal_name="primary",
            metric_value=v, threshold=c.threshold,
            status=_status(ok),
            detail=f"{c.metric}({c.window}) {c.op} {c.threshold}",
        ))
    for i, s in enumerate(secondary if isinstance(secondary, list) else []):
        c = Condition.model_validate(s)
        ok, v = c.evaluate(cards[(c.window, c.source)])
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
        ok, v = gr.evaluate(cards[(gr.window, gr.source)])
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

    # Promotion: pick the ladder stage matching the agent's CURRENT mode. ALL
    # conditions must explicitly pass (na blocks promotion), the agent must
    # have spent min_days_in_mode there, a fresh G0 confirmation must exist
    # when required, and no guardrail may be failing. Risk controls dominate
    # growth controls: an agent cannot be paused/demoted and promoted at once.
    stage = next((p for p in g.ladder() if p.from_mode == mode), None)
    if stage and not guardrail_failed:
        blockers: list[str] = []
        start = _mode_start_ms(conn, g.agent, last_promoted_ms)
        days_in_mode = (now_ms - start) / 86_400_000 if start else 0.0
        if days_in_mode < stage.min_days_in_mode:
            blockers.append(
                f"only {days_in_mode:.1f}d in {mode} (< {stage.min_days_in_mode:g}d)")
        if stage.require_g0 and not g0_confirmed(
                conn, g.agent, max_age_days=stage.g0_max_age_days, now_ms=now_ms,
                params_hash=params_hash):
            suffix = f" for deployed params {params_hash}" if params_hash else ""
            blockers.append(
                f"no fresh G0 confirmation (≤{stage.g0_max_age_days:g}d){suffix}")
        results = [c.evaluate(cards[(c.window, c.source)]) for c in stage.conditions]
        conditions_pass = bool(results) and all(ok is True for ok, _ in results)
        # Persistence: record this look's readiness, then require an unbroken
        # pass streak spanning persist_days with persist_evals looks.
        ready_name = f"promotion_ready:{stage.from_mode}->{stage.to_mode}"
        out.append(Evaluation(
            agent=g.agent, goal_name=ready_name,
            metric_value=None, threshold=None,
            status="pass" if conditions_pass else "fail",
            detail="conditions snapshot for the persistence gate",
        ))
        if conditions_pass:
            n_passes, streak_days = _ready_streak(conn, g.agent, ready_name, now_ms)
            n_passes += 1  # include this (not-yet-persisted) look
            if n_passes < stage.persist_evals or streak_days < stage.persist_days:
                blockers.append(
                    f"persistence {n_passes}/{stage.persist_evals} looks over "
                    f"{streak_days:.1f}/{stage.persist_days:g}d")
        if conditions_pass and not blockers:
            out.append(Evaluation(
                agent=g.agent, goal_name="promotion",
                metric_value=None, threshold=None,
                status="pass", action="promote",
                detail=f"{stage.from_mode} -> {stage.to_mode}",
                to_mode=stage.to_mode,
            ))
        elif conditions_pass and blockers:
            out.append(Evaluation(
                agent=g.agent, goal_name="promotion",
                metric_value=None, threshold=None,
                status="na", action="none",
                detail=f"{stage.from_mode} -> {stage.to_mode} blocked: "
                       + "; ".join(blockers),
            ))

    return out


def _ready_streak(
    conn: sqlite3.Connection, agent: str, ready_name: str, now_ms: int,
) -> tuple[int, float]:
    """(consecutive passing looks, days the streak spans) for a promotion
    stage's readiness snapshots — any 'fail' look breaks the streak."""
    rows = conn.execute(
        """SELECT ts_ms, status FROM goal_evaluations
           WHERE agent = ? AND goal_name = ?
           ORDER BY ts_ms DESC LIMIT 500""",
        (agent, ready_name),
    ).fetchall()
    n, oldest = 0, now_ms
    for r in rows:
        if r["status"] != "pass":
            break
        n += 1
        oldest = int(r["ts_ms"])
    return n, (now_ms - oldest) / 86_400_000 if n else 0.0


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
