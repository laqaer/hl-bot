"""Maker execution lifecycle — track post-only orders across ticks.

Taker orders fill (or don't) in the same tick; maker (post-only) orders *rest*
and fill later, so the live loop needs a small state machine spanning ticks:

  submit post-only  ──► log 'rest' (coin, side, sz, px, cloid, oid)
  next ticks:        ──► if a fill with that cloid appears  → log 'place' (owned)
                     ──► if it's been resting too long       → cancel + log 'cancel'

Ownership (``bot_owned_coins``) still keys off 'place'/'flatten', so a resting
order is NOT treated as a position until it actually fills. The logic here is
pure (DB + dict in, decisions out) so it's unit-testable without an exchange; the
thin exchange calls (place_limit_order / cancel_order) live in ``orders.py``.

Default execution stays taker; this path is opt-in and human-gated (see
docs/GO_LIVE.md). Its first live use should be watched at tiny size.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from ..agents.decisions import Decision, log_decision

log = logging.getLogger(__name__)

DEFAULT_MAX_REST_S = 1800  # cancel a maker quote unfilled after 30 min


def maker_price(
    side: str,
    book: tuple[float, float] | None,
    fallback_px: float,
) -> float:
    """Price a post-only quote off the live book: join the touch.

    Buys post at best bid, sells at best ask — never crosses, so the post-only
    order can't be rejected for crossing, and the quote sits at the front of
    the book instead of at a possibly-stale REST mid (the old behavior, which
    either rested far from touch and never filled, or crossed and got
    rejected). Falls back to the agent's intended price when no fresh book is
    available (e.g. WS snapshot stale or coin not subscribed).
    """
    if book:
        bid, ask = book
        if bid > 0 and ask > 0 and bid <= ask:
            return bid if side == "B" else ask
    return fallback_px


def log_rest(
    conn: sqlite3.Connection, agent: str, coin: str, side: str, sz: float,
    px: float, cloid: str, oid: int | None,
) -> None:
    """Record a resting post-only order (not yet a position)."""
    log_decision(conn, Decision(
        agent=agent, action="rest", coin=coin, side=side, sz=sz, px=px, cloid=cloid,
        reasoning=f"MAKER resting {side} {coin} {sz}@{px} oid={oid}",
        market_snapshot={"oid": oid, "resting": True}, is_paper=False,
    ))


def working_orders(conn: sqlite3.Connection, agent: str) -> dict[str, dict[str, Any]]:
    """Coins with a still-working maker order (rested, not filled/cancelled).

    Resolution is by cloid: a 'rest' opens a working order; a 'place' (fill
    detected) or 'cancel' with the same cloid closes it. Returns coin -> info.
    """
    import json

    rows = conn.execute(
        """SELECT ts_ms, coin, action, side, sz, px, cloid, market_snapshot
           FROM agent_decisions
           WHERE agent=? AND action IN ('rest','place','cancel') AND cloid IS NOT NULL
           ORDER BY ts_ms ASC""",
        (agent,),
    ).fetchall()
    by_cloid: dict[str, dict[str, Any]] = {}
    for r in rows:
        cloid = r["cloid"]
        if r["action"] == "rest":
            oid = None
            try:
                oid = (json.loads(r["market_snapshot"]) or {}).get("oid") if r["market_snapshot"] else None
            except (ValueError, TypeError):
                oid = None
            by_cloid[cloid] = {
                "coin": r["coin"], "side": r["side"], "sz": float(r["sz"] or 0),
                "px": float(r["px"] or 0), "ts_ms": int(r["ts_ms"]), "cloid": cloid, "oid": oid,
            }
        else:  # place (filled) or cancel resolves the resting order
            by_cloid.pop(cloid, None)
    # last write wins per coin
    out: dict[str, dict[str, Any]] = {}
    for info in by_cloid.values():
        out[info["coin"]] = info
    return out


def reconcile_maker_fills(
    conn: sqlite3.Connection, agent: str, working: dict[str, dict[str, Any]]
) -> list[str]:
    """For working orders whose cloid now appears in ``fills``, log a 'place'
    (with the REAL fill px/sz) so the position becomes owned. Returns filled coins.

    Requires fills to be ingested (cloid-attributed) before calling.
    """
    filled: list[str] = []
    for coin, o in working.items():
        row = conn.execute(
            "SELECT px, sz FROM fills WHERE cloid = ? ORDER BY time_ms ASC LIMIT 1",
            (o["cloid"],),
        ).fetchone()
        if not row:
            continue
        log_decision(conn, Decision(
            agent=agent, action="place", coin=coin, side=o["side"],
            sz=float(row["sz"] or o["sz"]), px=float(row["px"] or o["px"]), cloid=o["cloid"],
            reasoning=f"MAKER FILLED {coin} (resting order cloid {o['cloid'][:10]}…)",
            is_paper=False,
        ))
        filled.append(coin)
    return filled


def stale_working(
    working: dict[str, dict[str, Any]], *, now_s: float | None = None,
    max_rest_s: int = DEFAULT_MAX_REST_S,
) -> list[dict[str, Any]]:
    """Working orders that have rested longer than ``max_rest_s`` -> cancel."""
    now_s = now_s if now_s is not None else time.time()
    return [o for o in working.values() if now_s - o["ts_ms"] / 1000 > max_rest_s]


def log_cancel(conn: sqlite3.Connection, agent: str, o: dict[str, Any]) -> None:
    log_decision(conn, Decision(
        agent=agent, action="cancel", coin=o["coin"], side=o["side"], sz=o["sz"],
        px=o["px"], cloid=o["cloid"],
        reasoning=f"MAKER cancel stale resting {o['coin']} oid={o.get('oid')}",
        is_paper=False,
    ))
