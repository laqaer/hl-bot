"""Fills -> positions replay (B9 / REVIEW M2).

The ``positions`` table is the *logical per-agent* view of who owns what among
our agents. It is derived purely from ``fills`` (the exchange's ground truth),
so attribution survives partial fills and manual interference — unlike the
decision-log heuristic, which only knows binary owned/not-owned.

For each ``(agent, coin)`` we replay fills in time order and maintain:
  - ``net_sz``        signed size (+ long, - short),
  - ``avg_entry_px``  size-weighted average entry of the *open* position,
  - ``realized_pnl``  accumulated exchange ``closed_pnl`` (never invented here),
  - ``fees_paid``     accumulated fees,
  - ``last_update_ms``time of the latest fill.

Realized PnL is taken straight from ``fills.closed_pnl`` — we reconcile against
the exchange and never compute PnL from our own records.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

# Treat a residual smaller than this (in coins) as a flat position, so float
# noise from partial fills doesn't leave a phantom dust position open.
_EPS = 1e-9


@dataclass
class PositionState:
    agent: str
    coin: str
    net_sz: float = 0.0
    avg_entry_px: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    last_update_ms: int = 0


def _apply_fill(
    st: PositionState,
    side: str,
    px: float,
    sz: float,
    closed_pnl: float,
    fee: float,
    t_ms: int,
) -> None:
    """Fold one fill into a running position state (in place)."""
    signed = sz if side == "B" else -sz
    prev = st.net_sz
    new = prev + signed
    if abs(new) < _EPS:
        new = 0.0

    increasing = prev == 0.0 or (prev > 0) == (signed > 0)
    if increasing:
        denom = abs(prev) + abs(signed)
        st.avg_entry_px = (
            (abs(prev) * st.avg_entry_px + abs(signed) * px) / denom if denom else 0.0
        )
    elif new == 0.0:
        # Fully closed -> no open position, so no entry price.
        st.avg_entry_px = 0.0
    elif (prev > 0) != (new > 0):
        # Flipped through zero -> the residual was opened at this fill's price.
        st.avg_entry_px = px
    # else: reduced but same side -> the remaining lot keeps its avg entry.

    st.net_sz = new
    st.realized_pnl += float(closed_pnl)
    st.fees_paid += float(fee)
    st.last_update_ms = max(st.last_update_ms, int(t_ms))


def replay_positions(fills: list[dict]) -> dict[tuple[str, str], PositionState]:
    """Replay time-ordered fills into per-(agent, coin) position states.

    ``fills`` is a list of mappings with keys ``agent, coin, side, px, sz,
    closed_pnl, fee, time_ms``. Caller is responsible for ordering by time.
    """
    states: dict[tuple[str, str], PositionState] = {}
    for f in fills:
        agent = f["agent"] or "manual"
        coin = f["coin"]
        key = (agent, coin)
        st = states.get(key)
        if st is None:
            st = states[key] = PositionState(agent=agent, coin=coin)
        _apply_fill(
            st,
            side=f["side"],
            px=float(f["px"]),
            sz=float(f["sz"]),
            closed_pnl=float(f.get("closed_pnl", 0) or 0),
            fee=float(f.get("fee", 0) or 0),
            t_ms=int(f["time_ms"]),
        )
    return states


def rebuild_positions(conn: sqlite3.Connection) -> int:
    """Rebuild the ``positions`` table from all fills. Returns rows written.

    Idempotent: replays the full fills history each call (fills are upserted, so
    this is cheap and always reflects the latest ground truth). Safe to run after
    every ingest.
    """
    rows = conn.execute(
        """SELECT agent, coin, side, px, sz, closed_pnl, fee, time_ms
           FROM fills WHERE coin IS NOT NULL
           ORDER BY time_ms ASC, tid ASC"""
    ).fetchall()
    states = replay_positions([dict(r) for r in rows])

    now_ms = int(time.time() * 1000)
    conn.execute("DELETE FROM positions")
    for st in states.values():
        conn.execute(
            """INSERT INTO positions(
                agent, coin, net_sz, avg_entry_px, realized_pnl,
                fees_paid, last_update_ms
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                st.agent,
                st.coin,
                st.net_sz,
                st.avg_entry_px,
                st.realized_pnl,
                st.fees_paid,
                st.last_update_ms or now_ms,
            ),
        )
    return len(states)
