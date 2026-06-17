"""Persist G0 confirmation results to SQLite for audit and auto-promotion."""

from __future__ import annotations

import json
import sqlite3
import time

from .confirm import ConfirmationResult


def save_confirmation_result(
    conn: sqlite3.Connection,
    result: ConfirmationResult,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> int:
    """Write a ConfirmationResult to the `confirmation_results` table.

    Returns the inserted row id.
    """
    ts = int(time.time() * 1000)
    raw = {
        "in_sample": {
            "name": result.in_sample.name,
            "net_pnl": result.in_sample.net_pnl,
            "edge_bps": result.in_sample.edge_bps,
            "sharpe": result.in_sample.sharpe,
            "n_trades": result.in_sample.n_trades,
        },
        "out_of_sample": {
            "name": result.out_of_sample.name,
            "net_pnl": result.out_of_sample.net_pnl,
            "edge_bps": result.out_of_sample.edge_bps,
            "sharpe": result.out_of_sample.sharpe,
            "n_trades": result.out_of_sample.n_trades,
        },
        "cost_ladder": [
            {
                "name": s.name,
                "net_pnl": s.net_pnl,
                "edge_bps": s.edge_bps,
                "sharpe": s.sharpe,
                "n_trades": s.n_trades,
            }
            for s in result.cost_ladder
        ],
        "robust_to_2x_slippage": result.robust_to_2x_slippage,
        "min_is_trades": result.min_is_trades,
        "min_oos_trades": result.min_oos_trades,
    }
    cur = conn.execute(
        """
        INSERT INTO confirmation_results(
            ts_ms, agent, params_hash, window_start_ms, window_end_ms,
            prefer, confirmed, reasons, is_edge_bps, oos_edge_bps, oos_sharpe,
            is_trades, oos_trades, robust_2x, raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ts,
            result.agent,
            result.params_hash,
            window_start_ms,
            window_end_ms,
            result.prefer,
            1 if result.confirmed else 0,
            json.dumps(result.reasons),
            result.in_sample.edge_bps,
            result.out_of_sample.edge_bps,
            result.out_of_sample.sharpe,
            result.in_sample.n_trades,
            result.out_of_sample.n_trades,
            1 if result.robust_to_2x_slippage else 0,
            json.dumps(raw, separators=(",", ":")),
        ),
    )
    return cur.lastrowid or 0
