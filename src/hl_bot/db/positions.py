"""Replay fills into the per-agent `positions` table (REVIEW M2 / backlog B9).

Per-agent attribution elsewhere (funding split in `scoring.metrics`) is inferred
from the decision log as a binary owned/not-owned flag, so it can't see partial
fills or size drift. The exchange `fills` stream is the ground truth for *size*,
so we replay it into the `positions` table — net size, size-weighted average
entry, realized PnL (taken from the exchange's `closed_pnl`, never recomputed),
and fees — keyed by (agent, coin). This survives partial fills and manual
interference because it is derived purely from executed trades.

The replay is a pure function (`replay_positions`) so it is unit-testable without
a DB; `rebuild_positions` materializes the result into the table on ingest.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

# A fill row, in the column order `rebuild_positions` selects. Tests pass tuples.
#   (agent, coin, side, px, sz, closed_pnl, fee, time_ms)
FillRow = tuple[str, str, str, float, float, float, float, int]


@dataclass
class PositionRow:
    agent: str
    coin: str
    net_sz: float = 0.0          # + long, - short
    avg_entry_px: float = 0.0    # size-weighted entry of the *open* portion
    realized_pnl: float = 0.0    # sum of exchange closed_pnl
    fees_paid: float = 0.0
    last_update_ms: int = 0


def _apply_fill(pos: PositionRow, side: str, px: float, sz: float,
                closed_pnl: float, fee: float, t_ms: int) -> None:
    """Fold one fill into a running (agent, coin) position, in place.

    `side` is Hyperliquid's 'B' (buy → +) / 'A' (sell → −). Average entry is
    recomputed only when the position grows in its current direction; reducing
    leaves it untouched, and a flip through zero re-bases it to the fill price.
    Realized PnL is the exchange's, summed — we never invent PnL internally.
    """
    signed = sz if side == "B" else -sz
    if pos.net_sz == 0 or (pos.net_sz > 0) == (signed > 0):
        # Opening or adding in the same direction: size-weighted average entry.
        new_net = pos.net_sz + signed
        pos.avg_entry_px = (
            (pos.avg_entry_px * abs(pos.net_sz) + px * abs(signed)) / abs(new_net)
        )
        pos.net_sz = new_net
    else:
        # Reducing, closing, or flipping the position.
        new_net = pos.net_sz + signed
        if abs(signed) >= abs(pos.net_sz) and new_net != 0:
            pos.avg_entry_px = px      # flipped: remainder opens at this fill
        elif new_net == 0:
            pos.avg_entry_px = 0.0     # fully closed
        # pure reduction leaves avg_entry_px unchanged
        pos.net_sz = new_net

    pos.realized_pnl += closed_pnl
    pos.fees_paid += fee
    pos.last_update_ms = t_ms


def replay_positions(fills: Iterable[FillRow]) -> dict[tuple[str, str], PositionRow]:
    """Replay time-ordered fills into per-(agent, coin) positions.

    Callers must pass fills sorted ascending by time; `rebuild_positions` does.
    """
    out: dict[tuple[str, str], PositionRow] = {}
    for agent, coin, side, px, sz, closed_pnl, fee, t_ms in fills:
        if agent is None or coin is None:
            continue
        key = (agent, coin)
        pos = out.get(key)
        if pos is None:
            pos = out[key] = PositionRow(agent=agent, coin=coin)
        _apply_fill(pos, side, float(px), float(sz),
                    float(closed_pnl or 0.0), float(fee or 0.0), int(t_ms))
    return out


def rebuild_positions(conn: sqlite3.Connection) -> int:
    """Rebuild the entire `positions` table from `fills`. Returns rows written.

    Idempotent: a full DELETE + re-insert, so re-running after new fills lands
    yields the correct current state without drift.
    """
    rows = conn.execute(
        """SELECT agent, coin, side, px, sz, closed_pnl, fee, time_ms
           FROM fills WHERE agent IS NOT NULL AND coin IS NOT NULL
           ORDER BY time_ms ASC, tid ASC"""
    ).fetchall()
    positions = replay_positions(rows)
    cur = conn.cursor()
    cur.execute("DELETE FROM positions")
    cur.executemany(
        """INSERT INTO positions(
               agent, coin, net_sz, avg_entry_px, realized_pnl, fees_paid, last_update_ms
           ) VALUES(?,?,?,?,?,?,?)""",
        [
            (p.agent, p.coin, p.net_sz, p.avg_entry_px,
             p.realized_pnl, p.fees_paid, p.last_update_ms)
            for p in positions.values()
        ],
    )
    return len(positions)
