"""Allocator packet — the complete capital-decision deliverable in one bundle.

Path C (``docs/ROADMAP_TO_1M.md``) needs an allocator (or the supervisor's own
go-live gate) to see three things together, not as scattered files:

1. **Chassis** — the safety/accounting/supervision/risk machinery the strategy
   would be deployed *on*. The REVIEW verdict is that this is the strong part of
   the repo, and it is what makes a future positive edge deployable at scale
   rationally. Each item cites its source module so the claim is auditable.
2. **Track record** — the live, ground-truth numbers (equity curve, per-agent
   net/edge/Sharpe/$DD), built from the same tables ``score_agent`` uses live so
   it can never flatter reality.
3. **Edge search** — the honest negative result: the ten structurally-different
   theses searched and why each was pruned, plus why the search is exhausted.

This module composes the two existing pure reports (``track_record`` +
``edge_search``) and adds the frozen chassis record; it introduces no new
numbers of its own. The point is a single ``allocator_packet.{json,md}`` an
outside party can read end-to-end: *here is the machine, here is its live
record, here is everything we tried and rejected.*
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .edge_search import build_edge_search
from .edge_search import to_markdown as edge_search_markdown
from .track_record import build_track_record
from .track_record import to_markdown as track_record_markdown


@dataclass(frozen=True)
class ChassisItem:
    """One audited strength of the deployment chassis (REVIEW "What's good")."""

    name: str
    detail: str
    source: str  # module path, checkable against the repo


# The chassis strengths, transcribed from docs/REVIEW.md "What's good (keep it)".
# Each source path is a real module in this repo (verified at authoring time).
CHASSIS: tuple[ChassisItem, ...] = (
    ChassisItem(
        name="Cloid attribution",
        detail="The logical agent id is packed into the client order id, so every "
        "exchange fill attributes back to the agent that placed it — for live and "
        "reconciliation alike.",
        source="src/hl_bot/agents/cloid.py",
    ),
    ChassisItem(
        name="Ground-truth accounting",
        detail="PnL is reconciled from exchange userFills, never invented internally; "
        "the track record below is computed from these same tables.",
        source="src/hl_bot/ingest/hyperliquid.py",
    ),
    ChassisItem(
        name="Order safety",
        detail="The executor inspects statuses[].filled (no phantom 'ok'), retries "
        "with backoff, enforces a per-coin cooldown, reconciles against live position "
        "state, rounds size to szDecimals, and checks 0600 key-file permissions.",
        source="src/hl_bot/exec/orders.py",
    ),
    ChassisItem(
        name="Supervisor semantics",
        detail="A missing metric (N/A) never triggers an action and risk controls "
        "dominate promotion — a strategy can only be scaled on evidence, never on a gap.",
        source="src/hl_bot/supervisor/goals.py",
    ),
    ChassisItem(
        name="Risk scaling",
        detail="Portfolio-aware 5x-total / 1x-per-position notional caps layered with "
        "per-agent configured caps; pure and unit-tested. Risk changes are tightening-only.",
        source="src/hl_bot/risk/scaling.py",
    ),
    ChassisItem(
        name="Research hygiene",
        detail="Strategy health strips the single best coin from the 'core edge' so one "
        "lucky coin can't make a bleeding book look healthy; only ever proposes "
        "risk-reducing changes.",
        source="src/hl_bot/research/strategy_health.py",
    ),
)

# The one-line honest summary an allocator reads first.
HEADLINE = (
    "Strong deployment chassis (safety / ground-truth accounting / supervision / "
    "risk-scaling), an honest live track record, and a completed ten-thesis edge "
    "search that found NO net-of-cost edge on HL majors+alts. Capital is NOT "
    "warranted until a strategy clears the durability bar (G0-G3); this packet is "
    "the evidence for that gate, not a solicitation."
)


def build_allocator_packet(
    conn: sqlite3.Connection,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Assemble the full allocator packet: chassis + track record + edge search."""
    now_ms = now_ms or int(time.time() * 1000)
    return {
        "generated_ms": now_ms,
        "headline": HEADLINE,
        "chassis": [asdict(c) for c in CHASSIS],
        "track_record": build_track_record(conn, now_ms=now_ms),
        "edge_search": build_edge_search(),
    }


def to_markdown(packet: dict[str, Any]) -> str:
    lines = [
        "# hl-bot allocator packet",
        "",
        packet["headline"],
        "",
        "## Deployment chassis",
        "What a future positive edge would be deployed *on*. Each item is auditable "
        "against the cited source module.",
        "",
        "| strength | detail | source |",
        "|---|---|---|",
    ]
    for c in packet["chassis"]:
        lines.append(f"| {c['name']} | {c['detail']} | `{c['source']}` |")
    lines += [
        "",
        "---",
        "",
        track_record_markdown(packet["track_record"]),
        "",
        "---",
        "",
        edge_search_markdown(packet["edge_search"]),
    ]
    return "\n".join(lines)


def export(conn: sqlite3.Connection, out_dir: str | Path) -> tuple[Path, Path]:
    """Write allocator_packet.{json,md}; return their paths."""
    packet = build_allocator_packet(conn)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "allocator_packet.json"
    mp = d / "allocator_packet.md"
    jp.write_text(json.dumps(packet, indent=2))
    mp.write_text(to_markdown(packet))
    return jp, mp
