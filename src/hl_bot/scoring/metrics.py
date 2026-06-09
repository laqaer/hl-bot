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

import math
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
    # Account-level funding (everything). Per-agent attribution is below.
    q = "SELECT COALESCE(SUM(usdc), 0) FROM funding_payments"
    params: list = []
    if since_ms is not None:
        q += " WHERE time_ms >= ?"
        params.append(since_ms)
    return float(conn.execute(q, params).fetchone()[0])


_HOLD_INF = 1 << 62


def _coin_holders_over_time(
    conn: sqlite3.Connection,
) -> dict[str, list[tuple[str, int, int]]]:
    """Reconstruct which agent held which coin when, from the decision audit log.

    Returns coin -> list of (agent, open_ms, close_ms) intervals. A `place`
    opens an interval for (agent, coin); a `flatten` closes it. Still-open
    positions run to +inf. This is the basis for attributing account-level
    funding payments back to the agent that actually held the position.
    """
    rows = conn.execute(
        """SELECT ts_ms, agent, action, coin FROM agent_decisions
           WHERE action IN ('place', 'flatten') AND coin IS NOT NULL
           ORDER BY ts_ms ASC"""
    ).fetchall()
    open_pos: dict[tuple[str, str], int] = {}
    intervals: dict[str, list[tuple[str, int, int]]] = {}
    for r in rows:
        key = (r["agent"], r["coin"])
        if r["action"] == "place":
            open_pos.setdefault(key, int(r["ts_ms"]))
        else:  # flatten
            if key in open_pos:
                intervals.setdefault(r["coin"], []).append(
                    (r["agent"], open_pos.pop(key), int(r["ts_ms"]))
                )
    for (agent, coin), o in open_pos.items():
        intervals.setdefault(coin, []).append((agent, o, _HOLD_INF))
    return intervals


def _agent_funding_payments(
    conn: sqlite3.Connection, agent: str, since_ms: int | None
) -> list[tuple[int, float]]:
    """This agent's attributed funding payments as (time_ms, usdc_share).

    Each account-level funding payment is split equally among the agents holding
    that coin at that instant, so shares sum to the total without double-counting.
    Coins held only by manual positions stay unattributed (counted under _account).
    """
    intervals = _coin_holders_over_time(conn)
    q = "SELECT time_ms, coin, usdc FROM funding_payments"
    params: list = []
    if since_ms is not None:
        q += " WHERE time_ms >= ?"
        params.append(since_ms)
    out: list[tuple[int, float]] = []
    for r in conn.execute(q, params).fetchall():
        t = int(r["time_ms"])
        coin = r["coin"]
        usdc = float(r["usdc"] or 0.0)
        holders = [ag for (ag, o, c) in intervals.get(coin, []) if o <= t < c]
        if agent in holders:
            out.append((t, usdc / len(holders)))
    return out


def _daily_pnl_sharpe(daily: list[float], periods_per_year: float = 365) -> float | None:
    """Annualized Sharpe from a daily-PnL series (dollar terms), ≥3 days required.

    Matches the MetaAllocator's convention so per-agent Sharpe is consistent
    across the system. Dollar-PnL Sharpe is dimensionless and comparable to the
    return-based account Sharpe when PnL is roughly stationary.
    """
    if len(daily) < 3:
        return None
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / len(daily)
    std = math.sqrt(var)
    return (mean / std * math.sqrt(periods_per_year)) if std > 0 else None


def _daily_pnl_drawdown(
    daily: list[float], capital_base: float, periods_per_year: float = 365
) -> tuple[float | None, float | None]:
    """Max drawdown (fraction) and Calmar for a per-agent daily-PnL series.

    Builds a synthetic equity curve ``capital_base + cumsum(daily_pnl)`` so the
    drawdown is expressed as a *fraction of capital* — the same units the account
    curve uses, which is what drawdown guardrails (e.g. ``>= -0.10``) compare
    against. Without this, per-agent ``max_drawdown`` is always None and any
    drawdown guardrail is permanently N/A (can never fire). Needs a positive
    base and ≥3 days of PnL.
    """
    if len(daily) < 3 or capital_base <= 0:
        return None, None
    equity = [capital_base]
    cum = capital_base
    for x in daily:
        cum += x
        equity.append(cum)
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v - peak) / peak)
    dd = max_dd if max_dd < 0 else 0.0
    rets = [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] != 0
    ]
    calmar = None
    if dd < 0 and rets:
        ann_ret = (1 + sum(rets) / len(rets)) ** periods_per_year - 1
        calmar = ann_ret / abs(dd)
    return dd, calmar


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


def score_agent(
    conn: sqlite3.Connection,
    agent: str,
    window: Window,
    capital_base: float | None = None,
) -> Scorecard:
    now_ms = int(time.time() * 1000)
    w = WINDOW_MS[window]
    since = now_ms - w if w else None

    fills = _fills_df(conn, agent, since)
    n_trades = int(len(fills))
    realized = float(fills["closed_pnl"].sum()) if n_trades else 0.0
    fees = float(fills["fee"].sum()) if n_trades else 0.0
    # Funding: account-level gets everything; a real agent gets its attributed
    # share (so a carry strategy's main revenue line is no longer invisible).
    if agent == "_account":
        fund_payments: list[tuple[int, float]] = []
        funding = _funding_total(conn, since)
    else:
        fund_payments = _agent_funding_payments(conn, agent, since)
        funding = sum(s for _, s in fund_payments)
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

    # Sharpe / DD
    sharpe = dd = calmar = None
    if agent == "_account":
        # Account uses the real equity curve (return-based Sharpe + % drawdown).
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
    else:
        # Per-agent Sharpe from daily PnL (trading + attributed funding), so
        # sharpe-based promotion gates can actually evaluate for real agents.
        daily: dict[int, float] = {}
        if n_trades:
            for tm, cp, fe in zip(fills["time_ms"], fills["closed_pnl"], fills["fee"], strict=False):
                d = int(tm) // 86_400_000
                daily[d] = daily.get(d, 0.0) + float(cp) - float(fe)
        for t, s in fund_payments:
            d = t // 86_400_000
            daily[d] = daily.get(d, 0.0) + s
        daily_series = [daily[k] for k in sorted(daily)]
        sharpe = _daily_pnl_sharpe(daily_series)
        # With a capital base, also compute fractional drawdown/Calmar so
        # max_drawdown guardrails can fire for real agents (not just _account).
        if capital_base is not None:
            dd, calmar = _daily_pnl_drawdown(daily_series, capital_base)

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
