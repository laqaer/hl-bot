"""Paper-book scorecards — replay the paper decision log into Scorecard shape.

``score_agent`` is fills-based, and a paper book produces no fills — so every
goals/promotion metric for a paper-only agent is permanently N/A and the
supervisor cannot read the forward-test evidence the paper book records
(B-PAPER3). This module replays an agent's paper decision book (``is_paper=1``
``place``/``flatten`` rows, the same replay semantics the agents themselves
use) into synthetic fills, with execution modeled by the backtester's
``CostModel`` (taker fees + slippage by default) so paper numbers are directly
comparable to G0 backtest numbers, and aggregates them into the same
``Scorecard`` shape ``score_agent`` produces.

Honest limits:

- ``funding_pnl`` is *modeled*, not paid: when the caller supplies funding-rate
  history (``funding_by_coin``), each hourly HL funding row during a hold
  accrues ``-signed_size × entry_px × rate`` — the backtest engine's accrual,
  except marked at the entry mid instead of an hourly mark (fine at femr's
  hours-scale holds, drifts on multi-day trend holds). Without rates (offline)
  funding stays 0; do not judge a funding strategy (femr) on a funding=0 card.
- Realized-only, matching the live scorecard: an entry contributes its fee and
  notional when placed, price PnL only on flatten. Open positions are listed
  separately (``paper_open_positions``), not marked to market.
- Exit fidelity is the agent's own: a position the agent never flattens never
  realizes PnL here either. (femr's paper exits run since B-PAPER2 — paper
  ticks feed its exit logic a position view synthesized from the paper book,
  see ``runtime.synthesize_paper_positions``.)
- Measurement only. Promotion to live stays human-gated; nothing here feeds
  auto-promotion.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any

from ..backtest.engine import CostModel
from .metrics import WINDOW_MS, Scorecard, Window, _daily_pnl_drawdown, _daily_pnl_sharpe


@dataclass(frozen=True)
class PaperFill:
    """A synthetic fill from replaying one paper decision row."""

    ts_ms: int
    coin: str
    side: str            # 'B' / 'A' (exit side is the opposite of the entry)
    sz: float
    px: float            # effective fill price (slippage applied for takers)
    fee: float
    closed_pnl: float    # 0.0 for entries; price PnL for exits


@dataclass(frozen=True)
class PaperPosition:
    """A still-open paper position at the end of the replay."""

    coin: str
    side: str
    sz: float
    entry_px: float      # effective (slippage applied)
    entry_ts_ms: int


def _entry_px(px: float, side: str, cost: CostModel) -> float:
    # Takers cross the spread: buys fill above mid, sells below (engine semantics).
    return px * (1.0 + cost.slip) if side == "B" else px * (1.0 - cost.slip)


def replay_paper_fills(
    rows: list[tuple[int, str, str, str | None, float | None, float | None]],
    cost: CostModel | None = None,
) -> tuple[list[PaperFill], list[PaperPosition]]:
    """Replay one agent's (ts_ms, coin, action, side, sz, px) rows into fills.

    Same book semantics as the agents' own ``_position_state`` replays: a
    ``place`` opens (a re-place on a held coin overwrites — the agent's view of
    its book), a ``flatten`` closes the full held size at the logged mid. Rows
    that can't fill (missing px/sz/side, flatten with nothing open) are skipped.
    Returns (fills in ts order, positions still open at the end).
    """
    cost = cost or CostModel()
    fills: list[PaperFill] = []
    open_by_coin: dict[str, PaperPosition] = {}
    for ts_ms, coin, action, side, sz, px in rows:
        if action == "place":
            if side not in ("B", "A") or not sz or sz <= 0 or not px or px <= 0:
                continue
            eff = _entry_px(px, side, cost)
            fills.append(PaperFill(
                ts_ms=ts_ms, coin=coin, side=side, sz=sz, px=eff,
                fee=eff * sz * cost.fee_rate, closed_pnl=0.0,
            ))
            open_by_coin[coin] = PaperPosition(
                coin=coin, side=side, sz=sz, entry_px=eff, entry_ts_ms=ts_ms)
        elif action == "flatten":
            pos = open_by_coin.pop(coin, None)
            if pos is None or not px or px <= 0:
                continue
            close_side = "A" if pos.side == "B" else "B"
            eff = _entry_px(px, close_side, cost)
            price_pnl = (
                (eff - pos.entry_px) * pos.sz if pos.side == "B"
                else (pos.entry_px - eff) * pos.sz
            )
            fills.append(PaperFill(
                ts_ms=ts_ms, coin=coin, side=close_side, sz=pos.sz, px=eff,
                fee=eff * pos.sz * cost.fee_rate, closed_pnl=price_pnl,
            ))
    return fills, list(open_by_coin.values())


@dataclass(frozen=True)
class PaperHold:
    """One span the paper book held a coin — the unit funding accrues over."""

    coin: str
    side: str            # 'B' / 'A'
    sz: float
    entry_px: float      # raw logged mid — funding marks notional, no slippage
    entry_ts_ms: int
    exit_ts_ms: int | None   # None = still open


def replay_paper_holds(
    rows: list[tuple[int, str, str, str | None, float | None, float | None]],
) -> list[PaperHold]:
    """Replay rows into hold spans, mirroring ``replay_paper_fills`` book rules.

    A re-place on a held coin DROPS the old hold (its exit never fills there,
    so it accrues nothing here either); a flatten always ends the hold even
    with a missing px (the book closes although no exit fill is produced);
    unfillable places open nothing. Open holds get ``exit_ts_ms=None``.
    """
    holds: list[PaperHold] = []
    open_by_coin: dict[str, PaperHold] = {}
    for ts_ms, coin, action, side, sz, px in rows:
        if action == "place":
            if side not in ("B", "A") or not sz or sz <= 0 or not px or px <= 0:
                continue
            open_by_coin[coin] = PaperHold(
                coin=coin, side=side, sz=sz, entry_px=px,
                entry_ts_ms=ts_ms, exit_ts_ms=None)
        elif action == "flatten":
            pos = open_by_coin.pop(coin, None)
            if pos is not None:
                holds.append(replace(pos, exit_ts_ms=ts_ms))
    holds.extend(open_by_coin.values())
    return holds


def modeled_funding_events(
    holds: list[PaperHold],
    funding_by_coin: dict[str, list[dict[str, Any]]],
    now_ms: int,
) -> list[tuple[int, float]]:
    """Modeled funding cash flows ``(time_ms, usdc)`` over the paper holds.

    Each HL ``fundingHistory`` row is one hourly payment: a hold present at the
    row's timestamp pays/collects ``-signed_size × entry_px × rate`` (the
    engine's accrual, marked at the entry mid). Rows strictly after entry and
    at-or-before exit count; an open hold accrues up to ``now_ms`` — funding
    is cash at its own timestamp, like live ``funding_payments`` rows, not a
    close-time realization.
    """
    out: list[tuple[int, float]] = []
    for h in holds:
        end = h.exit_ts_ms if h.exit_ts_ms is not None else now_ms
        signed = h.sz if h.side == "B" else -h.sz
        for row in funding_by_coin.get(h.coin, ()):
            t = int(row.get("time", 0) or 0)
            if t <= h.entry_ts_ms or t > end:
                continue
            rate = float(row.get("fundingRate", 0) or 0)
            if rate:
                out.append((t, -signed * h.entry_px * rate))
    out.sort(key=lambda e: e[0])
    return out


def paper_funding_spans(
    conn: sqlite3.Connection, now_ms: int | None = None
) -> dict[str, tuple[int, int]]:
    """Per coin, the (start_ms, end_ms) funding-history span that covers every
    paper agent's holds (open holds run to now) — what a caller must fetch to
    model accrual for the whole paper book."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    spans: dict[str, tuple[int, int]] = {}
    for agent in list_paper_agents(conn):
        for h in replay_paper_holds(_paper_rows(conn, agent)):
            end = h.exit_ts_ms if h.exit_ts_ms is not None else now_ms
            cur = spans.get(h.coin)
            spans[h.coin] = (
                min(cur[0], h.entry_ts_ms) if cur else h.entry_ts_ms,
                max(cur[1], end) if cur else end,
            )
    return spans


def _paper_rows(
    conn: sqlite3.Connection, agent: str
) -> list[tuple[int, str, str, str | None, float | None, float | None]]:
    rows = conn.execute(
        """SELECT ts_ms, coin, action, side, sz, px FROM agent_decisions
           WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
             AND is_paper=1
           ORDER BY ts_ms ASC""",
        (agent,),
    ).fetchall()
    return [(int(r["ts_ms"]), r["coin"], r["action"], r["side"], r["sz"], r["px"])
            for r in rows]


def score_paper_agent(
    conn: sqlite3.Connection,
    agent: str,
    window: Window,
    cost: CostModel | None = None,
    capital_base: float | None = None,
    now_ms: int | None = None,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
) -> Scorecard:
    """Score an agent's paper book over ``window``, in ``score_agent``'s shape.

    The full book is replayed (entries must pair with exits across the window
    boundary); only fills inside the window are aggregated — same semantics as
    the fills-based scorecard, where each leg counts when it happens. With
    ``funding_by_coin`` (raw ``fetch_funding_history`` rows covering the holds,
    see ``paper_funding_spans``), funding accrual is modeled per hourly event
    and window-filtered by event time, like live ``funding_payments``; without
    it funding is 0 (offline).
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    w = WINDOW_MS[window]
    since = now_ms - w if w else None

    rows = _paper_rows(conn, agent)
    all_fills, _ = replay_paper_fills(rows, cost)
    fills = [f for f in all_fills if since is None or f.ts_ms >= since]

    fund_events: list[tuple[int, float]] = []
    if funding_by_coin:
        fund_events = [
            (t, u)
            for t, u in modeled_funding_events(
                replay_paper_holds(rows), funding_by_coin, now_ms)
            if since is None or t >= since
        ]

    n_trades = len(fills)
    realized = sum(f.closed_pnl for f in fills)
    fees = sum(f.fee for f in fills)
    funding = sum(u for _, u in fund_events)
    net = realized + funding - fees

    closes = [f.closed_pnl for f in fills if f.closed_pnl != 0]
    wins = [p for p in closes if p > 0]
    losses = [p for p in closes if p < 0]
    win_rate = len(wins) / len(closes) if closes else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses
        else float("inf") if wins else 0.0
    )

    notional = sum(f.px * f.sz for f in fills)
    edge_bps = net / notional * 10_000 if notional > 0 else None

    daily: dict[int, float] = {}
    for f in fills:
        d = f.ts_ms // 86_400_000
        daily[d] = daily.get(d, 0.0) + f.closed_pnl - f.fee
    for t, u in fund_events:
        d = t // 86_400_000
        daily[d] = daily.get(d, 0.0) + u
    daily_series = [daily[k] for k in sorted(daily)]
    sharpe = _daily_pnl_sharpe(daily_series)
    dd = calmar = None
    if capital_base is not None:
        dd, calmar = _daily_pnl_drawdown(daily_series, capital_base)

    return Scorecard(
        agent=agent, window=window, n_trades=n_trades,
        realized_pnl=realized, fees_paid=fees, funding_pnl=funding, net_pnl=net,
        win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
        sharpe=sharpe, max_drawdown=dd, calmar=calmar,
        notional_traded=notional, edge_bps=edge_bps,
    )


def paper_open_positions(
    conn: sqlite3.Connection, agent: str, cost: CostModel | None = None
) -> list[PaperPosition]:
    """Positions the agent's paper book still holds (entry-effective prices)."""
    _, open_pos = replay_paper_fills(_paper_rows(conn, agent), cost)
    return sorted(open_pos, key=lambda p: p.coin)


def list_paper_agents(conn: sqlite3.Connection) -> list[str]:
    """Agents with any executable paper rows (the paper book's roster)."""
    rows = conn.execute(
        """SELECT DISTINCT agent FROM agent_decisions
           WHERE is_paper=1 AND coin IS NOT NULL
             AND action IN ('place','flatten')"""
    ).fetchall()
    return sorted(r[0] for r in rows)


def score_paper_all(
    conn: sqlite3.Connection,
    windows: list[Window] | None = None,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
) -> list[Scorecard]:
    windows = windows or ["24h", "7d", "30d", "all"]
    out: list[Scorecard] = []
    for a in list_paper_agents(conn):
        for w in windows:
            out.append(score_paper_agent(conn, a, w, funding_by_coin=funding_by_coin))
    return out
