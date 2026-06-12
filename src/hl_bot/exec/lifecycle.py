"""Maker order lifecycle v2 — book-aware quoting + a real state machine.

The v1 path (exec/maker.py) could rest a quote and cancel it when stale, but
had no repricing, no partial-fill handling, and no escalation for exits. This
module owns the full lifecycle on the ``maker_orders`` table:

    QUOTED ──► FILLED            (cloid appears in fills for the full size)
           ──► PARTIAL           (some size filled; remainder keeps resting)
           ──► REPRICED          (market moved: cancel, requote at the touch)
           ──► EXPIRED           (rested too long: cancel, give up)
           ──► TAKER_FALLBACK    (exit/stop overdue: cross the spread NOW)

Planning (`plan_actions`) is pure — rows + fills + view in, actions out — so
every transition is unit-testable without an exchange. `apply_actions` is the
thin side-effect layer over exec/orders.py primitives. Ownership semantics are
unchanged: a resting quote is NOT a position; `log_rest`/`log_cancel` and the
fill-time 'place' audit rows keep `bot_owned_coins` truthful.

Risk-reduction beats price: exit/stop orders are placed reduce-only post-only
first, but escalate to a taker close after ``exit_timeout_s`` — an unfilled
exit is an unmanaged position.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..agents.base import MarketView
from ..agents.decisions import Decision, log_decision
from .maker import log_cancel, log_rest
from .orders import (
    OrderResult,
    cancel_order,
    close_position,
    place_limit_order,
    round_price_to,
)

log = logging.getLogger(__name__)

OPEN_STATES = ("quoted", "partial")


@dataclass(frozen=True)
class MakerConfig:
    reprice_bps: float = 5.0        # requote when the touch moved this far away
    min_requote_s: float = 30.0     # but never churn faster than this
    max_rest_s: float = 900.0       # give up on an unfilled ENTRY after this
    max_reprices: int = 8
    exit_timeout_s: float = 120.0   # unfilled exit/stop escalates to taker
    fallback_offset_bps: float = 1.0  # quote offset from mid when no book


@dataclass
class MakerAction:
    kind: Literal["fill", "partial", "reprice", "expire", "taker_fallback"]
    order: dict[str, Any]
    filled_sz: float = 0.0
    fill_px: float = 0.0
    new_px: float | None = None


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


def price_quote(
    view: MarketView, coin: str, side: str, cfg: MakerConfig,
    sz_decimals: int = 5,
) -> float | None:
    """Passive quote price: join the touch when the WS book is available,
    otherwise rest ``fallback_offset_bps`` inside the mid. Returns None when
    the coin has no usable price."""
    book = view.book_top.get(coin)
    if book:
        bid, ask = book
        if side == "B" and bid > 0:
            return round_price_to(bid, sz_decimals)
        if side == "A" and ask > 0:
            return round_price_to(ask, sz_decimals)
    mid = view.mids.get(coin)
    if mid is None or mid <= 0:
        return None
    off = cfg.fallback_offset_bps / 10_000.0
    px = mid * (1 - off) if side == "B" else mid * (1 + off)
    return round_price_to(px, sz_decimals)


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


def open_orders(conn: sqlite3.Connection, agent: str | None = None) -> list[dict[str, Any]]:
    q = f"SELECT * FROM maker_orders WHERE state IN ({','.join('?' for _ in OPEN_STATES)})"
    params: list[Any] = list(OPEN_STATES)
    if agent:
        q += " AND agent = ?"
        params.append(agent)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def record_quote(
    conn: sqlite3.Connection, *, cloid: str, agent: str, coin: str, side: str,
    sz: float, limit_px: float, oid: int | None, urgency: str = "normal",
    reduce_only: bool = False, now_ms: int | None = None,
    parent_cloid: str | None = None, reprice_count: int = 0,
) -> None:
    ts = now_ms or int(time.time() * 1000)
    conn.execute(
        """INSERT OR REPLACE INTO maker_orders
           (cloid, agent, coin, side, sz, filled_sz, limit_px, oid, state,
            urgency, reduce_only, created_ms, updated_ms, reprice_count, parent_cloid)
           VALUES(?,?,?,?,?,0,?,?,'quoted',?,?,?,?,?,?)""",
        (cloid, agent, coin, side, sz, limit_px, oid, urgency,
         1 if reduce_only else 0, ts, ts, reprice_count, parent_cloid),
    )
    log_rest(conn, agent, coin, side, sz, limit_px, cloid, oid)


def _set_state(
    conn: sqlite3.Connection, cloid: str, state: str, *,
    filled_sz: float | None = None, now_ms: int | None = None,
) -> None:
    ts = now_ms or int(time.time() * 1000)
    if filled_sz is not None:
        conn.execute(
            "UPDATE maker_orders SET state=?, filled_sz=?, updated_ms=? WHERE cloid=?",
            (state, filled_sz, ts, cloid),
        )
    else:
        conn.execute(
            "UPDATE maker_orders SET state=?, updated_ms=? WHERE cloid=?",
            (state, ts, cloid),
        )


def fills_by_cloid(conn: sqlite3.Connection, cloids: list[str]) -> dict[str, tuple[float, float]]:
    """cloid -> (total filled sz, avg px) from the ingested fills table."""
    if not cloids:
        return {}
    placeholders = ",".join("?" for _ in cloids)
    rows = conn.execute(
        f"""SELECT cloid, SUM(sz) AS sz, SUM(px * sz) / SUM(sz) AS avg_px
            FROM fills WHERE cloid IN ({placeholders}) GROUP BY cloid""",
        cloids,
    ).fetchall()
    return {r["cloid"]: (float(r["sz"] or 0), float(r["avg_px"] or 0)) for r in rows}


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------


def plan_actions(
    orders: list[dict[str, Any]],
    fills: dict[str, tuple[float, float]],
    view: MarketView,
    now_ms: int,
    cfg: MakerConfig,
) -> list[MakerAction]:
    """Decide what to do with every open maker order. Pure."""
    actions: list[MakerAction] = []
    for o in orders:
        filled, avg_px = fills.get(o["cloid"], (0.0, 0.0))
        remaining = float(o["sz"]) - filled
        age_s = (now_ms - int(o["created_ms"])) / 1000.0
        since_update_s = (now_ms - int(o["updated_ms"])) / 1000.0

        if filled > 0 and remaining <= float(o["sz"]) * 1e-6:
            actions.append(MakerAction("fill", o, filled_sz=filled, fill_px=avg_px))
            continue
        if filled > float(o["filled_sz"]) + 1e-12:
            actions.append(MakerAction("partial", o, filled_sz=filled, fill_px=avg_px))
            # partial remainder keeps resting on the exchange; fall through to
            # the staleness checks below for the remainder.

        is_exit = o["urgency"] in ("exit", "stop")
        if is_exit and age_s > cfg.exit_timeout_s:
            actions.append(MakerAction("taker_fallback", o, filled_sz=filled))
            continue
        if not is_exit and age_s > cfg.max_rest_s:
            actions.append(MakerAction("expire", o))
            continue

        quote = price_quote(view, o["coin"], o["side"], cfg)
        mid = view.mids.get(o["coin"])
        if quote is None or mid is None or mid <= 0:
            continue
        drift_bps = abs(quote - float(o["limit_px"])) / mid * 10_000
        if (
            drift_bps > cfg.reprice_bps
            and since_update_s > cfg.min_requote_s
            and int(o["reprice_count"]) < cfg.max_reprices
        ):
            actions.append(MakerAction("reprice", o, filled_sz=filled, new_px=quote))
    return actions


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def apply_actions(
    conn: sqlite3.Connection,
    exchange: Any,
    actions: list[MakerAction],
    *,
    now_ms: int | None = None,
) -> list[str]:
    """Execute planned transitions. Returns human-readable event strings."""
    from ..agents.cloid import make_cloid

    ts = now_ms or int(time.time() * 1000)
    events: list[str] = []
    for a in actions:
        o = a.order
        if a.kind == "fill":
            _set_state(conn, o["cloid"], "filled", filled_sz=a.filled_sz, now_ms=ts)
            log_decision(conn, Decision(
                agent=o["agent"], action="flatten" if o["reduce_only"] else "place",
                coin=o["coin"], side=o["side"], sz=a.filled_sz, px=a.fill_px,
                cloid=o["cloid"],
                reasoning=f"MAKER FILLED {o['coin']} {a.filled_sz}@{a.fill_px}",
                is_paper=False,
            ))
            events.append(f"FILLED {o['agent']} {o['coin']} {a.filled_sz}@{a.fill_px}")
        elif a.kind == "partial":
            _set_state(conn, o["cloid"], "partial", filled_sz=a.filled_sz, now_ms=ts)
            events.append(f"PARTIAL {o['agent']} {o['coin']} {a.filled_sz}/{o['sz']}")
        elif a.kind == "expire":
            _cancel(conn, exchange, o, "expired", ts)
            events.append(f"EXPIRED {o['agent']} {o['coin']}")
        elif a.kind == "reprice":
            _cancel(conn, exchange, o, "cancelled", ts)
            remaining = float(o["sz"]) - a.filled_sz
            new_cloid = make_cloid(o["agent"])
            res: OrderResult = place_limit_order(
                exchange, o["coin"], o["side"] == "B", remaining, a.new_px or 0,
                post_only=True, reduce_only=bool(o["reduce_only"]), cloid=new_cloid,
            )
            if res.status == "resting" or res.ok:
                record_quote(
                    conn, cloid=new_cloid, agent=o["agent"], coin=o["coin"],
                    side=o["side"], sz=remaining, limit_px=a.new_px or 0,
                    oid=res.oid, urgency=o["urgency"],
                    reduce_only=bool(o["reduce_only"]), now_ms=ts,
                    parent_cloid=o["cloid"],
                    reprice_count=int(o["reprice_count"]) + 1,
                )
                events.append(f"REPRICED {o['agent']} {o['coin']} -> {a.new_px}")
            else:
                events.append(f"REPRICE-FAILED {o['agent']} {o['coin']}: {res.error}")
        elif a.kind == "taker_fallback":
            _cancel(conn, exchange, o, "taker_fallback", ts)
            res = close_position(exchange, o["coin"], cloid=make_cloid(o["agent"]))
            if res.ok:
                log_decision(conn, Decision(
                    agent=o["agent"], action="flatten", coin=o["coin"],
                    px=res.avg_px, sz=res.filled_sz,
                    reasoning="MAKER exit timed out -> taker close",
                    is_paper=False,
                ))
                events.append(f"TAKER-CLOSE {o['agent']} {o['coin']} @ {res.avg_px}")
            else:
                events.append(f"TAKER-CLOSE-FAILED {o['agent']} {o['coin']}: {res.error}")
    conn.commit()
    return events


def _cancel(conn: sqlite3.Connection, exchange: Any, o: dict[str, Any], state: str, ts: int) -> None:
    if o.get("oid") is not None:
        cancel_order(exchange, o["coin"], int(o["oid"]))
    _set_state(conn, o["cloid"], state, now_ms=ts)
    log_cancel(conn, o["agent"], {**o, "px": o["limit_px"]})


def submit_entry(
    conn: sqlite3.Connection, exchange: Any, view: MarketView, d: Decision,
    cfg: MakerConfig, *, sz_decimals: int = 5, now_ms: int | None = None,
) -> str:
    """Quote a new maker order for an agent decision. Returns an event string."""
    if d.coin is None or not d.sz or d.side not in ("B", "A") or not d.cloid:
        return f"SKIP {d.agent} {d.coin}: malformed decision"
    px = price_quote(view, d.coin, d.side, cfg, sz_decimals)
    if px is None:
        return f"SKIP {d.agent} {d.coin}: no price"
    urgency = getattr(d, "urgency", "normal")
    reduce_only = d.action == "flatten" or urgency in ("exit", "stop")
    res = place_limit_order(
        exchange, d.coin, d.side == "B", d.sz, px,
        post_only=True, reduce_only=reduce_only, cloid=d.cloid,
    )
    if res.status == "resting":
        record_quote(conn, cloid=d.cloid, agent=d.agent, coin=d.coin, side=d.side,
                     sz=d.sz, limit_px=px, oid=res.oid, urgency=urgency,
                     reduce_only=reduce_only, now_ms=now_ms)
        return f"RESTING {d.agent} {d.coin} {d.side} {d.sz}@{px} oid={res.oid}"
    if res.ok:  # filled immediately (possible when not post-only-rejected)
        log_decision(conn, Decision(
            agent=d.agent, action="flatten" if reduce_only else "place",
            coin=d.coin, side=d.side, sz=res.filled_sz or d.sz,
            px=res.avg_px or px, cloid=d.cloid,
            reasoning=f"MAKER immediate fill {d.coin}", is_paper=False,
        ))
        return f"FILLED {d.agent} {d.coin} @ {res.avg_px}"
    # Audit the rejection — an unlogged reject would be retried every cycle
    # invisibly. A post-only reject ('rejected' from HL) means the touch
    # moved: logged as maker_reject so the agent may requote next cycle
    # without tripping coin_in_cooldown, but it still counts in
    # order_rate_ok so a requote storm hits the rate wall, not the exchange.
    # Anything else (bad size/px, exception) is a hard 'rejected' -> cooldown.
    action = "maker_reject" if res.status == "rejected" else "rejected"
    log_decision(conn, Decision(
        agent=d.agent, action=action, coin=d.coin, side=d.side,
        sz=d.sz, px=px, cloid=d.cloid,
        reasoning=f"MAKER entry rejected: {res.status}", error=res.error,
        is_paper=False,
    ))
    return f"REJECT {d.agent} {d.coin}: {res.status} — {res.error}"
