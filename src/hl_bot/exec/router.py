"""Decision execution router — the single live order-routing path.

Extracted from the CLI tick loop (REVIEW M3 / backlog B12) so the code that
turns agent Decisions into exchange orders is one audited, unit-testable
function instead of an inline loop. The router:

* routes each agent's ENTRIES per its execution mode — ``maker`` rests a
  post-only quote priced off the live book (join the touch via
  ``maker.maker_price``; falls back to the agent's intended price when the WS
  book is stale), ``taker`` crosses with a market order;
* always exits taker (``flatten`` → market close) — risk reduction must not
  wait in a queue;
* enforces the entry gates it is given (guardrails, per-coin cooldown, one
  working maker quote per coin) and logs ground-truth decisions only after
  exchange confirmation, with the REAL fill px/sz.

The exchange object is injected, so tests drive the full routing logic with a
fake. Network/SDK calls stay in ``orders.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..agents.decisions import Decision, log_decision
from .maker import log_rest, maker_price, working_orders
from .orders import (
    close_position,
    coin_in_cooldown,
    place_limit_order,
    place_market_order,
)

log = logging.getLogger(__name__)


@dataclass
class ExecOutcome:
    agent: str
    coin: str
    action: str            # decision action: place / flatten
    status: str            # filled | resting | closed | rejected | close_failed | skipped
    mode: str = "taker"    # execution mode used for entries
    px: float | None = None
    sz: float | None = None
    detail: str = ""


def execute_decisions(
    conn: sqlite3.Connection,
    exchange: Any,
    decisions: list[Decision],
    *,
    exec_modes: dict[str, str],
    entries_allowed: bool,
    book_top: dict[str, tuple[float, float]] | None = None,
) -> list[ExecOutcome]:
    """Execute place/flatten decisions for live agents. Returns outcomes.

    ``exec_modes`` maps agent name -> "maker"/"taker" for entries; agents not
    in the map are skipped entirely (not on the live roster). When
    ``entries_allowed`` is False (guardrail breach) entries are skipped but
    flattens still run — risk reduction is always allowed.
    """
    book_top = book_top or {}
    outcomes: list[ExecOutcome] = []
    # Working maker quotes per agent, refreshed lazily and kept current as we
    # rest new quotes during this loop.
    working_by_agent: dict[str, dict[str, dict]] = {}

    for d in decisions:
        if d.agent not in exec_modes or d.coin is None:
            continue
        mode = exec_modes[d.agent]

        if d.action == "place" and d.sz and d.side:
            if not entries_allowed:
                outcomes.append(ExecOutcome(d.agent, d.coin, "place", "skipped",
                                            mode, detail="guardrail blocks new entries"))
                continue
            if coin_in_cooldown(conn, d.coin, agent=d.agent):
                outcomes.append(ExecOutcome(d.agent, d.coin, "place", "skipped",
                                            mode, detail="in cooldown"))
                continue
            is_buy = (d.side == "B")

            if mode == "maker":
                if not d.cloid:
                    # place_limit_order would refuse anyway; surface it as a
                    # routing error rather than a cryptic order rejection.
                    outcomes.append(ExecOutcome(d.agent, d.coin, "place", "error",
                                                mode, detail="maker entry requires a cloid"))
                    continue
                if d.agent not in working_by_agent:
                    working_by_agent[d.agent] = working_orders(conn, d.agent)
                if d.coin in working_by_agent[d.agent]:
                    outcomes.append(ExecOutcome(d.agent, d.coin, "place", "skipped",
                                                mode, detail="maker quote already resting"))
                    continue
                quote_px = maker_price(d.side, book_top.get(d.coin), d.px or 0)
                res = place_limit_order(exchange, d.coin, is_buy, d.sz, quote_px,
                                        post_only=True, cloid=d.cloid)
                if res.status == "resting":
                    log_rest(conn, d.agent, d.coin, d.side, d.sz, quote_px, d.cloid, res.oid)
                    working_by_agent[d.agent][d.coin] = {"cloid": d.cloid, "oid": res.oid}
                    outcomes.append(ExecOutcome(d.agent, d.coin, "place", "resting",
                                                mode, px=quote_px, sz=d.sz,
                                                detail=f"oid={res.oid}"))
                elif res.ok:  # filled immediately (rare for post-only)
                    if res.avg_px:
                        d.px = res.avg_px
                    if res.filled_sz:
                        d.sz = res.filled_sz
                    log_decision(conn, d)
                    outcomes.append(ExecOutcome(d.agent, d.coin, "place", "filled",
                                                mode, px=d.px, sz=d.sz))
                else:
                    # Audited under its own action: a post-only reject just
                    # means the touch moved, so unlike taker 'rejected' it is
                    # not in coin_in_cooldown's action set — the agent may
                    # re-quote next tick.
                    log_decision(conn, Decision(
                        agent=d.agent, action="maker_reject", coin=d.coin,
                        side=d.side, sz=d.sz, px=quote_px,
                        reasoning=f"post-only reject: {res.error}", is_paper=False,
                    ))
                    outcomes.append(ExecOutcome(d.agent, d.coin, "place", "rejected",
                                                mode, detail=f"{res.status}: {res.error}"))
                conn.commit()
                continue

            res = place_market_order(exchange, d.coin, is_buy, d.sz,
                                     slippage_pct=0.01, cloid=d.cloid)
            if res.ok:
                # Log place ONLY after fill confirmed, with the REAL fill px/sz
                # (not the pre-trade mid) so downstream stops/TPs key off truth.
                if res.avg_px:
                    d.px = res.avg_px
                if res.filled_sz:
                    d.sz = res.filled_sz
                log_decision(conn, d)
                outcomes.append(ExecOutcome(d.agent, d.coin, "place", "filled",
                                            mode, px=d.px, sz=d.sz))
            else:
                log_decision(conn, Decision(
                    agent=d.agent, action="rejected", coin=d.coin,
                    reasoning=f"HL rejected: {res.error}", is_paper=False,
                ))
                outcomes.append(ExecOutcome(d.agent, d.coin, "place", "rejected",
                                            mode, detail=f"{res.status}: {res.error}"))
            conn.commit()

        elif d.action == "flatten":
            res = close_position(exchange, d.coin, cloid=d.cloid)
            if res.ok:
                # Log the flatten immediately so ownership clears this tick rather
                # than waiting for next-tick reconciliation. Record the real exit px.
                if res.avg_px:
                    d.px = res.avg_px
                log_decision(conn, d)
                outcomes.append(ExecOutcome(d.agent, d.coin, "flatten", "closed",
                                            mode, px=d.px))
            else:
                outcomes.append(ExecOutcome(d.agent, d.coin, "flatten", "close_failed",
                                            mode, detail=str(res.error)))
            conn.commit()

    return outcomes
