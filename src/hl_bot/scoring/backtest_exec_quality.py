"""Execution-quality telemetry for simulated backtest fills.

The backtester already records every synthetic fill in the ``fills`` table.
This module turns those rows into taker-cost diagnostics so a sweep can report
not just whether an edge survives costs, but *how* the simulated execution
looked: average entry/exit slippage, fee rate, and the share of fills that paid
taker fees.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BacktestExecQuality:
    agent: str
    n_fills: int = 0
    n_entries: int = 0
    n_exits: int = 0
    n_taker: int = 0
    avg_entry_slip_bps: float | None = None
    avg_exit_slip_bps: float | None = None
    avg_fee_bps: float | None = None
    taker_pct: float | None = None
    # Raw per-fill data for deeper inspection / serialization.
    fills: list[dict] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_fills": self.n_fills,
            "n_entries": self.n_entries,
            "n_exits": self.n_exits,
            "n_taker": self.n_taker,
            "avg_entry_slip_bps": self.avg_entry_slip_bps,
            "avg_exit_slip_bps": self.avg_exit_slip_bps,
            "avg_fee_bps": self.avg_fee_bps,
            "taker_pct": self.taker_pct,
        }


def backtest_exec_quality(
    conn: sqlite3.Connection,
    agent: str,
) -> BacktestExecQuality:
    """Compute simulated fill-quality stats for ``agent`` in ``conn``.

    Expects backtest fills to carry a JSON blob in ``raw_json`` with keys
    ``mid`` (the frame mid at fill time), ``is_entry`` (bool), and
    ``is_taker`` (bool). Falls back gracefully when those keys are missing.
    """
    rows = conn.execute(
        """SELECT px, fee, sz, raw_json FROM fills
           WHERE agent = ? AND dir = 'backtest'""",
        (agent,),
    ).fetchall()

    q = BacktestExecQuality(agent=agent)
    if not rows:
        return q

    entry_slips: list[float] = []
    exit_slips: list[float] = []
    fee_bpss: list[float] = []

    for r in rows:
        px = float(r["px"] or 0)
        sz = float(r["sz"] or 0)
        fee = float(r["fee"] or 0)
        meta: dict = {}
        with contextlib.suppress(ValueError, TypeError):
            meta = json.loads(r["raw_json"] or "{}") or {}

        mid = meta.get("mid", px)
        is_entry = bool(meta.get("is_entry", False))
        is_taker = bool(meta.get("is_taker", False))

        q.n_fills += 1
        if is_entry:
            q.n_entries += 1
        else:
            q.n_exits += 1
        if is_taker:
            q.n_taker += 1

        notional = px * sz
        if mid and mid > 0 and px and sz:
            slip = abs(px - mid) / mid * 10_000.0
            if is_entry:
                entry_slips.append(slip)
            else:
                exit_slips.append(slip)
        if notional > 0 and fee:
            fee_bpss.append(fee / notional * 10_000.0)

    def _avg(vals: list[float]) -> float | None:
        return sum(vals) / len(vals) if vals else None

    q.avg_entry_slip_bps = _avg(entry_slips)
    q.avg_exit_slip_bps = _avg(exit_slips)
    q.avg_fee_bps = _avg(fee_bpss)
    q.taker_pct = q.n_taker / q.n_fills if q.n_fills else None
    return q
