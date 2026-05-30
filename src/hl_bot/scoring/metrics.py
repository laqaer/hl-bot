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
    n_trades: int
    realized_pnl: float
    fees_paid: float
    funding_pnl: float
    net_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe: float | None
    max_drawdown: float | None
    calmar: float | None
    notional_traded: float
    edge_bps: float | None      # net_pnl / notional_traded in basis points

    def as_dict(self) -> dict:
        return asdict(self)


def _fills_df(conn: sqlite3.Connection, agent: str, since_ms: int | None) -> pd.DataFrame:
    q = "SELECT time_ms, coin, side, px, sz, closed_pnl, fee FROM fills WHERE agent = ?"
    params: list = [agent]
    if since_ms is not None:
        q += " AND time_ms >= ?"
        params.append(since_ms)
    q += " ORDER BY time_ms ASC"
    return pd.read_sql_query(q, conn, params=params)


def _funding_total(conn: sqlite3.Connection, since_ms: int | None) -> float:
    # Funding isn't attributable to a specific agent unless we track positions
    # per agent over time. For now we report account-level funding under the
    # "_account" pseudo-agent and 0 for everyone else.
    q = "SELECT COALESCE(SUM(usdc), 0) FROM funding_payments"
    params: list = []
    if since_ms is not None:
        q += " WHERE time_ms >= ?"
        params.append(since_ms)
    return float(conn.execute(q, params).fetchone()[0])


def _equity_curve(conn: sqlite3.Connection, since_ms: int | None) -> pd.DataFrame:
    q = "SELECT ts_ms, account_value FROM equity_snapshots"
    params: list = []
    if since_ms is not None:
        q += " WHERE ts_ms >= ?"
        params.append(since_ms)
    q += " ORDER BY ts_ms ASC"
    return pd.read_sql_query(q, conn, params=params)


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


def score_agent(conn: sqlite3.Connection, agent: str, window: Window) -> Scorecard:
    now_ms = int(time.time() * 1000)
    w = WINDOW_MS[window]
    since = now_ms - w if w else None

    fills = _fills_df(conn, agent, since)
    n_trades = int(len(fills))
    realized = float(fills["closed_pnl"].sum()) if n_trades else 0.0
    fees = float(fills["fee"].sum()) if n_trades else 0.0
    funding = _funding_total(conn, since) if agent == "_account" else 0.0
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
    notional = float((fills["px"] * fills["sz"]).abs().sum()) if n_trades else 0.0
    edge_bps = float(net / notional * 10_000) if notional > 0 else None

    # Sharpe / DD: account-level only (we need an equity curve)
    sharpe = dd = calmar = None
    if agent == "_account":
        eq = _equity_curve(conn, since)
        if len(eq) >= 3:
            eq["ts"] = pd.to_datetime(eq["ts_ms"], unit="ms")
            eq = eq.set_index("ts")["account_value"].resample("1D").last().dropna()
            rets = eq.pct_change().dropna()
            sharpe = _sharpe(rets, 365)
            dd = _max_dd(eq)
            if dd is not None and dd < 0:
                ann_ret = (1 + rets.mean()) ** 365 - 1 if not rets.empty else 0
                calmar = float(ann_ret / abs(dd)) if dd != 0 else None

    return Scorecard(
        agent=agent, window=window, n_trades=n_trades,
        realized_pnl=realized, fees_paid=fees, funding_pnl=funding, net_pnl=net,
        win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
        sharpe=sharpe, max_drawdown=dd, calmar=calmar,
        notional_traded=notional, edge_bps=edge_bps,
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
