"""Maker quote pricing and shared audit helpers.

A 'rest' audit row records a working quote without position ownership.
A 'place' or 'cancel' row with the same cloid resolves that quote.
The current fill and expiry state machine lives in ``lifecycle.py``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..agents.decisions import Decision, log_decision


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


def log_cancel(conn: sqlite3.Connection, agent: str, o: dict[str, Any]) -> None:
    log_decision(conn, Decision(
        agent=agent, action="cancel", coin=o["coin"], side=o["side"], sz=o["sz"],
        px=o["px"], cloid=o["cloid"],
        reasoning=f"MAKER cancel stale resting {o['coin']} oid={o.get('oid')}",
        is_paper=False,
    ))
