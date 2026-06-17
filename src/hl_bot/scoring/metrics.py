"""Compute scorecards from the ground-truth tables.

For each agent we compute over rolling windows (1h, 24h, 7d, 30d, all):
  - realized_pnl (sum of fills.closed_pnl)
  - fees_paid (sum of fills.fee, USD-equivalent assumed for now)
  - funding_pnl (sum of funding_payments.usdc when agent attribution is known)
  - net_pnl = realized + funding - fees
  - n_trades, win_rate, avg_win, avg_loss, profit_factor
  - sharpe (daily-resampled), max_drawdown, calmar

Equity curve for Sharpe/DD is built from equity_snapshots when the agent is
"the whole account" (default) or from cumulative agent net_pnl when attribution
is partial. We expose both.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import pandas as pd

from .attribution import daily_pnl_series, funding_events_for_agent
from .curves import dollar_max_drawdown

Window = Literal["1h", "24h", "7d", "30d", "all"]
WINDOW_MS: dict[Window, int | None] = {
    "1h":   3_600_000,
    "24h":  86_400_000,
    "7d":   7 * 86_400_000,
    "30d":  30 * 86_400_000,
    "all":  None,
}


@dataclass
class Scorecard:
    agent: str
    window: str
    n_trades: int            # CLOSE events (round-trip legs) — the sample-size truth
    realized_pnl: float
    fees_paid: float
    funding_pnl: float
    net_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe: float | None
    max_drawdown: float | None          # fraction of equity (account-level only)
    calmar: float | None
    notional_traded: float
    edge_bps: float | None      # net_pnl / notional_traded in basis points
    max_drawdown_usd: float | None = None  # dollar peak-to-trough (all agents)
    n_fills: int = 0                       # raw fill count (ops display only)

    def as_dict(self) -> dict:
        return asdict(self)


Source = Literal["live", "paper"]
_FILLS_TABLE = {"live": "fills", "paper": "paper_fills"}


def _fills_df(
    conn: sqlite3.Connection, agent: str, since_ms: int | None, source: Source = "live"
) -> pd.DataFrame:
    table = _FILLS_TABLE[source]
    q = f"SELECT time_ms, coin, side, px, sz, closed_pnl, fee FROM {table} WHERE agent = ?"
    params: list = [agent]
    if since_ms is not None:
        q += " AND time_ms >= ?"
        params.append(since_ms)
    q += " ORDER BY time_ms ASC"
    return pd.read_sql_query(q, conn, params=params)


def _funding_total(conn: sqlite3.Connection, since_ms: int | None) -> float:
    """Account-level funding truth: the exact sum of funding_payments,
    reported under the "_account" pseudo-agent. Real agents get their share
    via the fills position-replay in scoring.attribution (REVIEW C4)."""
    q = "SELECT COALESCE(SUM(usdc), 0) FROM funding_payments"
    params: list = []
    if since_ms is not None:
        q += " WHERE time_ms >= ?"
        params.append(since_ms)
    return float(conn.execute(q, params).fetchone()[0])


def _agent_funding(
    conn: sqlite3.Connection, agent: str, since_ms: int | None, source: Source = "live"
) -> pd.DataFrame:
    """Per-agent funding rows (time_ms, usdc). Live rows come from the
    funding_attribution replay (B6); paper rows from the simulator's accrual."""
    table = "funding_attribution" if source == "live" else "paper_funding"
    q = f"SELECT time_ms, usdc FROM {table} WHERE agent = ?"
    params: list = [agent]
    if since_ms is not None:
        q += " AND time_ms >= ?"
        params.append(since_ms)
    q += " ORDER BY time_ms ASC"
    try:
        return pd.read_sql_query(q, conn, params=params)
    except pd.errors.DatabaseError:
        # Pre-migration DB (table missing): funding simply unattributed.
        return pd.DataFrame({"time_ms": [], "usdc": []})


def _equity_curve(conn: sqlite3.Connection, since_ms: int | None) -> pd.DataFrame:
    q = "SELECT ts_ms, account_value FROM equity_snapshots"
    params: list = []
    if since_ms is not None:
        q += " WHERE ts_ms >= ?"
        params.append(since_ms)
    q += " ORDER BY ts_ms ASC"
    return pd.read_sql_query(q, conn, params=params)


def _matched_entry_notional(fills: pd.DataFrame, since_ms: int | None = None) -> float:
    """FIFO-match closing fills to their entry lots and return matched entry notional.

    Fixes the window-boundary edge inflation (V4): a trade opened before the
    window and closed inside it carried its full closed_pnl against only the
    exit leg's notional. By pairing each close with the lots that opened it, the
    denominator becomes the true capital deployed in those round-trips.

    ``since_ms`` scopes the returned notional to round-trips whose CLOSE fill
    falls inside the window; the matching still uses the full prior history so
    straddling trades get their true entry notional.

    If no closes can be matched (e.g., test fixtures with only closing fills),
    returns 0 so the caller can fall back to the legacy notional_traded.
    """
    if fills.empty:
        return 0.0
    total = 0.0
    for _coin, cdf in fills.groupby("coin"):
        # lots stored as signed size (+ long, - short), px, fee
        lots: list[tuple[float, float, float]] = []
        for _, row in cdf.iterrows():
            side = row["side"]
            sz = float(row["sz"])
            px = float(row["px"])
            close_in_window = since_ms is None or int(row["time_ms"]) >= since_ms
            signed = sz if side == "B" else -sz
            net = sum(lot_[0] for lot_ in lots)
            if net == 0 or net * signed > 0:
                lots.append((signed, px, 0.0))  # fee not needed for notional
            else:
                remaining = sz
                while remaining > 1e-12 and lots:
                    lot_signed, lot_px, _ = lots[0]
                    lot_abs = abs(lot_signed)
                    match = min(remaining, lot_abs)
                    if close_in_window:
                        total += match * lot_px
                    if match >= lot_abs - 1e-12:
                        lots.pop(0)
                    elif lot_signed > 0:
                        lots[0] = (lot_signed - match, lot_px, 0.0)
                    else:
                        lots[0] = (lot_signed + match, lot_px, 0.0)
                    remaining -= match
                if remaining > 1e-12:
                    lots.append((signed / sz * remaining, px, 0.0))
    return total


def _sharpe(returns: pd.Series, periods_per_year: float) -> float | None:
    if returns.empty or returns.std(ddof=0) == 0:
        return None
    return float(returns.mean() / returns.std(ddof=0) * np.sqrt(periods_per_year))


def _max_dd(equity: pd.Series) -> float | None:
    if equity.empty:
        return None
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max.replace(0, np.nan)
    return float(dd.min()) if not dd.empty else None


def score_agent(
    conn: sqlite3.Connection, agent: str, window: Window, source: Source = "live"
) -> Scorecard:
    now_ms = int(time.time() * 1000)
    w = WINDOW_MS[window]
    since = now_ms - w if w else None

    fills = _fills_df(conn, agent, since, source)
    n_fills = int(len(fills))
    # Sample size = CLOSE events, not fills: a 2-leg carry round trip is 4
    # fills but only 2 closes; counting fills made the n_trades gates look
    # 2-4x stronger than the statistical evidence they actually had.
    n_trades = int((fills["closed_pnl"] != 0).sum()) if n_fills else 0
    realized = float(fills["closed_pnl"].sum()) if n_fills else 0.0
    fees = float(fills["fee"].sum()) if n_fills else 0.0
    if agent == "_account":
        # Account row keeps the exchange total (includes residual + manual).
        funding = _funding_total(conn, since) if source == "live" else 0.0
        funding_events: list[tuple[int, float]] = []
    elif source == "live":
        # Per-agent share of account funding via fills position replay (C4).
        # Computed once here; reused below for the agent's equity curve.
        funding_events = funding_events_for_agent(conn, agent, since)
        funding = float(sum(u for _, u in funding_events))
    else:
        # Paper funding comes from the simulator's hourly accrual table.
        fdf = _agent_funding(conn, agent, since, source)
        funding_events = [(int(t), float(u)) for t, u in zip(fdf["time_ms"], fdf["usdc"], strict=True)]
        funding = float(sum(u for _, u in funding_events))
    net = realized + funding - fees

    # Per-trade win stats (close events only)
    closes = fills[fills["closed_pnl"] != 0]
    wins = closes[closes["closed_pnl"] > 0]["closed_pnl"]
    losses = closes[closes["closed_pnl"] < 0]["closed_pnl"]
    win_rate = float(len(wins) / len(closes)) if len(closes) else 0.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    profit_factor = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf") if wins.sum() > 0 else 0.0

    # Notional & edge
    notional = float((fills["px"] * fills["sz"]).abs().sum()) if n_fills else 0.0
    # V4: match closes inside the window to their entry lots using full history,
    # so straddling round-trips don't pair full PnL with only the exit leg.
    all_fills = _fills_df(conn, agent, None, source) if n_fills else fills
    matched_notional = _matched_entry_notional(all_fills, since_ms=since) if n_fills else 0.0
    edge_denominator = matched_notional if matched_notional > 0 else notional
    edge_bps = float(net / edge_denominator * 10_000) if edge_denominator > 0 else None

    # Sharpe / DD. The account gets fractional DD from its real equity curve;
    # real agents (live AND paper) get Sharpe from daily net PnL (fills +
    # attributed/simulated funding) plus a dollar drawdown — an agent has no
    # capital base, so a fractional DD would be an invention (C5/B7).
    sharpe = dd = calmar = None
    dd_usd: float | None = None
    if agent == "_account":
        eq = _equity_curve(conn, since)
        if len(eq) >= 3:
            curve = [(int(r.ts_ms), float(r.account_value)) for r in eq.itertuples()]
            dd_usd = dollar_max_drawdown(curve)
            eq["ts"] = pd.to_datetime(eq["ts_ms"], unit="ms")
            eq = eq.set_index("ts")["account_value"].resample("1D").last().dropna()
            rets = eq.pct_change().dropna()
            sharpe = _sharpe(rets, 365)
            dd = _max_dd(eq)
            if dd is not None and dd < 0:
                ann_ret = (1 + rets.mean()) ** 365 - 1 if not rets.empty else 0
                calmar = float(ann_ret / abs(dd)) if dd != 0 else None
    else:
        # Built from the source-respecting fills frame (live `fills` or
        # `paper_fills`) so paper scorecards get the same treatment;
        # equivalent to agent_pnl_events() for live.
        events = [
            (int(t), float(p))
            for t, p in zip(fills["time_ms"], fills["closed_pnl"] - fills["fee"], strict=True)
        ]
        events.extend(funding_events)
        events.sort(key=lambda e: e[0])
        daily = daily_pnl_series(events)
        if len(daily) >= 3:
            s = pd.Series(daily)
            sharpe = _sharpe(s, 365)
        if events:
            # Cumulative PnL starts flat at 0 before the first event, so the
            # baseline point makes an opening loss count as drawdown.
            cum, curve = 0.0, [(events[0][0], 0.0)]
            for ts, delta in events:
                cum += delta
                curve.append((ts, cum))
            dd_usd = dollar_max_drawdown(curve)

    return Scorecard(
        agent=agent, window=window, n_trades=n_trades, n_fills=n_fills,
        realized_pnl=realized, fees_paid=fees, funding_pnl=funding, net_pnl=net,
        win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
        sharpe=sharpe, max_drawdown=dd, calmar=calmar,
        notional_traded=notional, edge_bps=edge_bps,
        max_drawdown_usd=dd_usd,
    )


def list_agents(conn: sqlite3.Connection) -> list[str]:
    """All agent names we've seen — from fills, decisions, or agent_state."""
    rows = conn.execute(
        """
        SELECT DISTINCT agent FROM (
            SELECT agent FROM fills WHERE agent IS NOT NULL
            UNION SELECT agent FROM agent_decisions
            UNION SELECT agent FROM agent_state
        ) WHERE agent IS NOT NULL
        """
    ).fetchall()
    agents = sorted({r[0] for r in rows})
    # Always include the synthetic account-level row.
    if "_account" not in agents:
        agents.insert(0, "_account")
    return agents


def score_all(conn: sqlite3.Connection, windows: list[Window] | None = None) -> list[Scorecard]:
    windows = windows or ["24h", "7d", "30d", "all"]
    out: list[Scorecard] = []
    for a in list_agents(conn):
        for w in windows:
            out.append(score_agent(conn, a, w))
    return out
