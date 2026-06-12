"""Operator mode changes: the validated, audited path for agent_state flips.

GO_LIVE.md's procedure for the most consequential operation in the system —
letting an agent place live orders — was raw SQL against the live DB: no
agent-name validation (a typo'd INSERT creates a dead row while the real agent
stays paper), no evidence readout, no audit trail, and no unpause path at all
(`_pause` sets enabled=0 and nothing in the codebase ever sets it back — even
the supervisor's promote upsert leaves `enabled` untouched).

This module is that gate made usable, with the risk asymmetry built in:

- Tightening (mode rank down, or disabling) always applies — same rule as
  everywhere else in the risk machinery.
- Loosening (mode rank up, or a change that makes the agent live-capable)
  requires explicit ``confirm``, moves ONE rank at a time
  (paper -> live_small -> live, mirroring ``_demote``'s ladder down), and is
  checked against the same evidence gates the supervisor's promotion path
  uses (``_evidence_blockers``): a blocked flip needs ``override_evidence``
  on top of ``confirm``, so going live against thin/dirty evidence is a
  two-flag decision that lands on the audit record.
- Every applied change writes a ``goal_evaluations`` row
  (``goal_name='operator'``), so human flips appear in the same audit trail
  as supervisor actions. Operator rows never match the clean-guardrail
  breach query (it filters ``goal_name LIKE 'guardrail:%'``).

Nothing here places orders or runs ticks; it edits ``agent_state`` exactly as
the documented SQL did, minus the foot-guns. Live trading stays human-gated:
this code path only ever runs when a human types the command and its flags.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .goals import (
    AgentGoals,
    _clean_guardrail_blockers,
    _evidence_blockers,
    _evidence_span_days,
    _has_paper_book,
)

MODE_RANK = {"paper": 0, "live_small": 1, "live": 2}


class OperatorError(ValueError):
    """A refused mode change; the message is the reason shown to the human."""


@dataclass(frozen=True)
class AgentStateRow:
    """Current agent_state row, or the implicit default for a missing one
    (mode='paper', enabled=1 — matching the schema default and
    ``filter_live_agents``)."""

    agent: str
    mode: str
    enabled: int
    paused_reason: str | None = None
    paused_at_ms: int | None = None
    exists: bool = True

    @property
    def live_capable(self) -> bool:
        return self.enabled == 1 and MODE_RANK.get(self.mode, 0) > 0


@dataclass(frozen=True)
class ModeChange:
    """A validated, not-yet-applied agent_state change."""

    agent: str
    old: AgentStateRow
    new_mode: str
    new_enabled: int
    direction: str            # 'loosen' / 'tighten' / 'neutral'
    blockers: list[str]       # evidence gates failing at plan time
    overrode_evidence: bool   # blockers were present and explicitly overridden
    unpauses: bool            # clears a recorded pause marker

    @property
    def detail(self) -> str:
        d = (
            f"operator mode change: {self.old.mode}/"
            f"{'on' if self.old.enabled else 'off'} -> "
            f"{self.new_mode}/{'on' if self.new_enabled else 'off'}"
        )
        if self.unpauses:
            d += f"; cleared pause ({self.old.paused_reason or 'no reason recorded'})"
        if self.overrode_evidence:
            d += "; OVERRODE evidence gates: " + "; ".join(self.blockers)
        return d


def current_state(conn: sqlite3.Connection, agent: str) -> AgentStateRow:
    row = conn.execute(
        """SELECT mode, enabled, paused_reason, paused_at_ms
           FROM agent_state WHERE agent=?""",
        (agent,),
    ).fetchone()
    if row is None:
        return AgentStateRow(agent=agent, mode="paper", enabled=1, exists=False)
    return AgentStateRow(
        agent=agent, mode=row["mode"], enabled=int(row["enabled"]),
        paused_reason=row["paused_reason"], paused_at_ms=row["paused_at_ms"],
    )


def list_states(
    conn: sqlite3.Connection, known_agents: set[str]
) -> list[AgentStateRow]:
    """Every known agent's effective state (missing rows shown as defaults)."""
    names = set(known_agents)
    names.update(
        r["agent"] for r in conn.execute("SELECT agent FROM agent_state").fetchall()
    )
    return [current_state(conn, a) for a in sorted(names)]


@dataclass(frozen=True)
class EvidenceReadout:
    """What the supervisor's evidence gates see for this agent right now."""

    book: str                 # 'paper' / 'fills'
    span_days: float
    breaches_30d: int         # pause/demote guardrail fails on record, 30d
    last_promotion_detail: str | None
    last_promotion_ts_ms: int | None


def evidence_readout(
    conn: sqlite3.Connection,
    agent: str,
    paper_conn: sqlite3.Connection | None = None,
) -> EvidenceReadout:
    """What the evidence gates see right now. ``paper_conn`` is the separate
    paper DB on split-book deployments (B-PAPERLOOP) — the paper book and the
    paper supervisor's audit trail live there while agent_state stays in
    ``conn``; breach counts and the last promotion evaluation are read from
    BOTH trails. Default None = single DB, behavior unchanged."""
    pconn = paper_conn if paper_conn is not None else conn
    conns = [conn] if pconn is conn else [conn, pconn]
    state = current_state(conn, agent)
    use_paper = state.mode == "paper" and _has_paper_book(pconn, agent)
    cutoff = int(time.time() * 1000 - 30 * 86_400_000)
    breaches = 0
    for c in conns:
        row = c.execute(
            """SELECT COUNT(*) AS n FROM goal_evaluations
               WHERE agent=? AND goal_name LIKE 'guardrail:%' AND status='fail'
                 AND action_taken IN ('pause','demote') AND ts_ms >= ?""",
            (agent, cutoff),
        ).fetchone()
        breaches += int(row["n"]) if row is not None else 0
    promo = None
    for c in conns:
        row = c.execute(
            """SELECT ts_ms, detail FROM goal_evaluations
               WHERE agent=? AND goal_name='promotion'
               ORDER BY ts_ms DESC LIMIT 1""",
            (agent,),
        ).fetchone()
        if row is not None and (promo is None or row["ts_ms"] > promo["ts_ms"]):
            promo = row
    return EvidenceReadout(
        book="paper" if use_paper else "fills",
        span_days=_evidence_span_days(pconn if use_paper else conn, agent, use_paper),
        breaches_30d=breaches,
        last_promotion_detail=promo["detail"] if promo is not None else None,
        last_promotion_ts_ms=int(promo["ts_ms"]) if promo is not None else None,
    )


def _entry_contract(
    contracts: list[AgentGoals], agent: str, to_mode: str
) -> AgentGoals | None:
    """The promotion contract guarding entry INTO ``to_mode`` for this agent."""
    for g in contracts:
        if g.agent == agent and g.promotion and g.promotion.to_mode == to_mode:
            return g
    return None


def plan_mode_change(
    conn: sqlite3.Connection,
    agent: str,
    *,
    known_agents: set[str],
    contracts: list[AgentGoals],
    mode: str | None = None,
    enabled: bool | None = None,
    confirm: bool = False,
    override_evidence: bool = False,
    paper_conn: sqlite3.Connection | None = None,
) -> ModeChange:
    """Validate a requested agent_state change; raise OperatorError to refuse.

    ``known_agents`` should be the union of the runtime roster, the config
    contracts' agent names, and existing agent_state rows — anything else is
    a typo waiting to strand the real agent in paper while a dead row goes
    live. ``contracts`` are the loaded configs/*.yaml goals; the contract
    whose promotion targets the requested mode supplies the evidence gates.

    ``paper_conn`` is the separate paper DB on split-book deployments
    (B-PAPERLOOP): the paper book + paper audit trail are judged there, and
    a pause/demote breach recorded in EITHER trail blocks. The applied
    change always lands on ``conn`` — the DB the live tick obeys.
    """
    if agent not in known_agents:
        raise OperatorError(
            f"unknown agent '{agent}' — known: " + ", ".join(sorted(known_agents))
        )
    old = current_state(conn, agent)
    new_mode = mode if mode is not None else old.mode
    if new_mode not in MODE_RANK:
        raise OperatorError(
            f"invalid mode '{new_mode}' — one of: " + ", ".join(MODE_RANK)
        )
    new_enabled = (1 if enabled else 0) if enabled is not None else old.enabled
    if new_mode == old.mode and new_enabled == old.enabled:
        raise OperatorError(
            f"no change: {agent} is already {old.mode}/"
            f"{'on' if old.enabled else 'off'}"
        )

    rank_up = MODE_RANK[new_mode] > MODE_RANK[old.mode]
    if MODE_RANK[new_mode] - MODE_RANK[old.mode] > 1:
        raise OperatorError(
            f"{old.mode} -> {new_mode} skips a rank: promote one step at a "
            "time (paper -> live_small -> live), with a watched window at each"
        )

    new = AgentStateRow(agent=agent, mode=new_mode, enabled=new_enabled)
    loosening = rank_up or (new.live_capable and not old.live_capable)
    tightening = (
        MODE_RANK[new_mode] < MODE_RANK[old.mode]
        or (old.live_capable and not new.live_capable)
    )
    direction = "loosen" if loosening else ("tighten" if tightening else "neutral")

    blockers: list[str] = []
    overrode = False
    if loosening:
        pconn = paper_conn if paper_conn is not None else conn
        contract = _entry_contract(contracts, agent, new_mode)
        if contract is None or contract.promotion is None:
            blockers.append(
                f"no promotion contract gates entry to {new_mode} in configs/"
            )
        else:
            use_paper = old.mode == "paper" and _has_paper_book(pconn, agent)
            evidence_conn = pconn if use_paper else conn
            blockers.extend(
                _evidence_blockers(evidence_conn, agent, contract.promotion, use_paper)
            )
            if pconn is not conn:
                # A breach recorded in the OTHER trail blocks too: a live
                # demotion must gate a paper-evidence re-promotion, and a
                # paper-book breach must gate a fills-evidence rank-up.
                other = conn if use_paper else pconn
                other_label = "live" if use_paper else "paper"
                blockers.extend(
                    f"{b} ({other_label} book)"
                    for b in _clean_guardrail_blockers(
                        other, agent, contract.promotion)
                )
        if not confirm:
            raise OperatorError(
                f"{agent}: {old.mode}/{'on' if old.enabled else 'off'} -> "
                f"{new_mode}/{'on' if new_enabled else 'off'} loosens live "
                "exposure — re-run with --confirm"
                + ("; evidence gates failing: " + "; ".join(blockers)
                   if blockers else "")
            )
        if blockers and not override_evidence:
            raise OperatorError(
                f"{agent}: evidence gates failing: " + "; ".join(blockers)
                + " — re-run with --override-evidence to flip anyway "
                "(goes on the audit record)"
            )
        overrode = bool(blockers)

    return ModeChange(
        agent=agent, old=old, new_mode=new_mode, new_enabled=new_enabled,
        direction=direction, blockers=blockers, overrode_evidence=overrode,
        unpauses=bool(
            new_enabled == 1
            and (old.paused_reason is not None or old.paused_at_ms is not None)
        ),
    )


def apply_mode_change(conn: sqlite3.Connection, change: ModeChange) -> None:
    """Apply a planned change: upsert agent_state + write the audit row."""
    ts = int(time.time() * 1000)
    promoted = MODE_RANK[change.new_mode] > MODE_RANK[change.old.mode]
    conn.execute(
        """
        INSERT INTO agent_state(agent, mode, enabled, last_promoted_ms, notes)
        VALUES(?,?,?,?,?)
        ON CONFLICT(agent) DO UPDATE SET
            mode = excluded.mode,
            enabled = excluded.enabled,
            last_promoted_ms = COALESCE(excluded.last_promoted_ms,
                                        agent_state.last_promoted_ms),
            notes = excluded.notes
        """,
        (change.agent, change.new_mode, change.new_enabled,
         ts if promoted else None, change.detail),
    )
    if change.unpauses:
        conn.execute(
            "UPDATE agent_state SET paused_reason=NULL, paused_at_ms=NULL"
            " WHERE agent=?",
            (change.agent,),
        )
    conn.execute(
        """INSERT INTO goal_evaluations(
               ts_ms, agent, goal_name, metric_value, threshold,
               status, action_taken, detail
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (ts, change.agent, "operator", None, None, "pass", "none", change.detail),
    )
