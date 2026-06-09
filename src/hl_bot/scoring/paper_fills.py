"""Paper forward-test scoring: simulate fills from the paper decision log.

Why this exists
---------------
A paper agent (``agent_state.mode == 'paper'``) emits ``place``/``flatten``
decisions into ``agent_decisions`` but never produces exchange ``fills`` — so
``score_agent`` (which reads the ``fills`` table) measures **zero trades and a
``None`` edge for a paper agent forever**. That makes the G1 forward-test gate
(edge >= +5bps, net >= $50, >= 150 trades over 30d) *unmeasurable* in paper: the
"G1 clock" has no hands. ``hlbot gate-progress`` shows the conditions permanently
N/A no matter how long the paper agent runs.

This module turns the logged paper decisions into *simulated* fills using the
**exact same cost accounting as the backtest engine** (``backtest.engine``'s
``CostModel`` + ``_open``/``_close`` price/slip/fee math), then scores them with
the unchanged production ``score_agent``. So:

* the paper forward-test is measured under the *same* model that produced the
  confirmed edge (e.g. trend_breakout_v1's +5.5bps maker over 180d), removing a
  deploy-vs-evidence drift of the same class the project keeps closing; and
* nothing is written to the live DB — the simulation runs in a throwaway
  in-memory connection and scoring is read-only.

Faithfulness note / known divergence
-------------------------------------
The backtest engine folds *funding* into a closed position's realized PnL. Live
paper decisions carry no funding rate, so this simulator books **price PnL + fees
only** (no funding). That is exact for price strategies (trend/breakout) and an
understatement for carry strategies — which is the honest direction. Funding for
paper carry would need the funding feed replayed per hold and is out of scope.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..backtest.engine import CostModel
from ..db.schema import init_db
from .metrics import Scorecard, Window, score_agent


@dataclass
class _Pos:
    side: str            # 'B' long / 'A' short
    sz: float            # absolute size
    entry_px: float


def _fill_row(
    agent: str, coin: str, side: str, sz: float, px: float,
    fee: float, closed_pnl: float, ts_ms: int,
) -> dict[str, Any]:
    """A ``fills``-table-shaped row (the columns ``score_agent`` reads)."""
    return {
        "agent": agent, "coin": coin, "side": side, "sz": sz, "px": px,
        "fee": fee, "closed_pnl": closed_pnl, "time_ms": ts_ms,
    }


def simulate_paper_fills(
    decisions: Iterable[dict[str, Any]], cost: CostModel, *, agent: str = "paper"
) -> list[dict[str, Any]]:
    """Replay paper ``place``/``flatten`` decisions into simulated fill rows.

    ``decisions`` must be ordered by time and each be a mapping with keys
    ``ts_ms, action, coin, side, sz, px`` (the ``agent_decisions`` columns). The
    price/slippage/fee accounting mirrors ``backtest.engine`` exactly so a paper
    forward-test and a backtest of the same decision stream agree:

    * an entry pays ``slip`` on the fill price and a ``fee`` on notional, and is
      recorded as a fill with ``closed_pnl == 0`` (so it counts as a trade, as in
      the backtest); a same-side re-entry averages into the position;
    * an opposite-side entry first closes up to the resting size (booking that
      close's own fill) then opens only the leftover, so a reduce/flip never
      double-counts fees/notional;
    * a ``flatten`` closes ``min(sz, held)`` at the slipped exit price, booking
      ``closed_pnl = price_pnl`` and its own fee.

    Funding is not modeled (see module docstring).
    """
    book: dict[str, _Pos] = {}
    fills: list[dict[str, Any]] = []

    def _open(coin: str, side: str, sz: float, px: float, ts: int) -> None:
        existing = book.get(coin)
        if existing and existing.side != side:
            # Opposite side: close up to the resting size first, then open the
            # leftover. Capture size BEFORE the close mutates the position.
            existing_sz = existing.sz
            _close(coin, min(sz, existing_sz), px, ts)
            open_sz = sz - existing_sz
            if open_sz <= 1e-12:
                return  # pure reduce / exact flat — the close booked it
        else:
            open_sz = sz
        fill_px = px * (1 + cost.slip) if side == "B" else px * (1 - cost.slip)
        fee = fill_px * open_sz * cost.fee_rate
        existing = book.get(coin)  # may have been removed by the close above
        if existing and existing.side == side:
            tot = existing.sz + open_sz
            existing.entry_px = (existing.entry_px * existing.sz + fill_px * open_sz) / tot
            existing.sz = tot
        else:
            book[coin] = _Pos(side=side, sz=open_sz, entry_px=fill_px)
        fills.append(_fill_row(agent, coin, side, open_sz, fill_px, fee, 0.0, ts))

    def _close(coin: str, sz: float | None, px: float, ts: int) -> None:
        pos = book.get(coin)
        if not pos:
            return
        close_sz = min(sz if sz else pos.sz, pos.sz)
        if close_sz <= 0:
            return
        if pos.side == "B":
            exit_px = px * (1 - cost.slip)
            price_pnl = (exit_px - pos.entry_px) * close_sz
            close_side = "A"
        else:
            exit_px = px * (1 + cost.slip)
            price_pnl = (pos.entry_px - exit_px) * close_sz
            close_side = "B"
        fee = exit_px * close_sz * cost.fee_rate
        fills.append(_fill_row(agent, coin, close_side, close_sz, exit_px, fee, price_pnl, ts))
        pos.sz -= close_sz
        if pos.sz <= 1e-12:
            book.pop(coin, None)

    for d in decisions:
        coin = d.get("coin")
        px = d.get("px")
        if not coin or not px:
            continue
        ts = int(d["ts_ms"])
        action = d.get("action")
        if action == "place":
            side = d.get("side")
            sz = d.get("sz")
            if not sz or side not in ("B", "A"):
                continue
            _open(coin, side, float(sz), float(px), ts)
        elif action == "flatten":
            sz = d.get("sz")
            _close(coin, float(sz) if sz else None, float(px), ts)
        # 'hold' / 'cancel' / 'error' / 'rest': nothing to execute.
    return fills


def _read_paper_decisions(conn: sqlite3.Connection, agent: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT ts_ms, action, coin, side, sz, px
             FROM agent_decisions
            WHERE agent = ? AND is_paper = 1 AND action IN ('place', 'flatten')
            ORDER BY ts_ms ASC""",
        (agent,),
    ).fetchall()
    return [
        {"ts_ms": r["ts_ms"], "action": r["action"], "coin": r["coin"],
         "side": r["side"], "sz": r["sz"], "px": r["px"]}
        for r in rows
    ]


def score_paper_forward(
    conn: sqlite3.Connection,
    agent: str,
    window: Window = "30d",
    cost: CostModel | None = None,
    capital_base: float | None = None,
) -> Scorecard:
    """Score a paper agent's forward-test by simulating fills from its decisions.

    Read-only w.r.t. ``conn``: the simulated fills are inserted into a throwaway
    in-memory DB and scored with the production ``score_agent``, so the live
    ground-truth tables are never touched. ``cost`` defaults to **maker** — the
    execution the passive strategies' confirmed edge was measured under; pass a
    taker ``CostModel`` for the honest taker comparison.
    """
    cost = cost or CostModel(maker=True)
    decisions = _read_paper_decisions(conn, agent)
    fills = simulate_paper_fills(decisions, cost, agent=agent)

    mem = init_db(":memory:")
    for i, f in enumerate(fills):
        h = f"paper-{agent}-{f['coin']}-{f['time_ms']}-{i}"
        mem.execute(
            """INSERT OR IGNORE INTO fills(
                hash, tid, time_ms, coin, side, px, sz, start_position, dir,
                closed_pnl, fee, fee_token, builder_fee, cloid, agent, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (h, i, f["time_ms"], f["coin"], f["side"], f["px"], f["sz"], 0,
             "paper-sim", f["closed_pnl"], f["fee"], "USDC", 0, None, agent, "{}"),
        )
    return score_agent(mem, agent, window, capital_base)
