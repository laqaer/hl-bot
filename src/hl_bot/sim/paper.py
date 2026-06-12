"""Paper-fill simulator — makes paper performance scoreable (and honest).

Paper agents used to produce only decision rows, never fills, so every
promotion gate (`n_trades`, `edge_bps`, `sharpe`) was structurally
unsatisfiable from paper mode. This module simulates execution of paper-mode
agents' decisions against the LIVE market view using the backtest engine's
cost conventions, writing:

  * ``paper_fills``    — simulated fills (taker pays fee+slippage; maker pays
                         maker fee, and a resting order fills only when price
                         CROSSES the limit — touch is not enough, deliberately
                         conservative so paper can't flatter maker fill rates);
  * ``paper_funding``  — hourly funding accrual on open paper positions (the
                         entire edge of the carry strategies);
  * ``agent_decisions``— the place/flatten audit rows (is_paper=1) the agents
                         themselves replay to know their own open positions.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ..agents.base import MarketView
from ..agents.decisions import Decision, log_decision
from ..backtest.engine import CostModel


@dataclass
class PaperCycleResult:
    fills: list[tuple[str, str, str, float, float]] = field(default_factory=list)  # agent, coin, side, sz, px
    rested: list[tuple[str, str, str, float, float]] = field(default_factory=list)
    funding_rows: int = 0

    def summary(self) -> str:
        return (f"paper: {len(self.fills)} fills, {len(self.rested)} resting, "
                f"{self.funding_rows} funding accruals")


# ---------------------------------------------------------------------------
# Simulated position state (replayed from paper_fills — same pattern agents
# use on agent_decisions, but with fill-grade px/sz truth)
# ---------------------------------------------------------------------------


def _positions(conn: sqlite3.Connection, agent: str) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT coin, side, px, sz, time_ms FROM paper_fills
           WHERE agent = ? ORDER BY time_ms ASC, id ASC""",
        (agent,),
    ).fetchall()
    pos: dict[str, dict] = {}
    for r in rows:
        coin = r["coin"]
        signed = float(r["sz"]) if r["side"] == "B" else -float(r["sz"])
        st = pos.setdefault(coin, {"net_sz": 0.0, "avg_entry_px": 0.0, "opened_ms": int(r["time_ms"])})
        prev = st["net_sz"]
        new = prev + signed
        if prev == 0:
            st["avg_entry_px"] = float(r["px"])
            st["opened_ms"] = int(r["time_ms"])
        elif prev * signed > 0:
            total = abs(prev) + abs(signed)
            st["avg_entry_px"] = (st["avg_entry_px"] * abs(prev) + float(r["px"]) * abs(signed)) / total
        elif new != 0 and prev * new < 0:
            st["avg_entry_px"] = float(r["px"])
            st["opened_ms"] = int(r["time_ms"])
        st["net_sz"] = new
        if new == 0:
            pos.pop(coin, None)
    return pos


def _write_fill(
    conn: sqlite3.Connection, *, ts_ms: int, agent: str, coin: str, side: str,
    px: float, sz: float, closed_pnl: float, fee: float,
    cloid: str | None, reasoning: str | None,
) -> None:
    conn.execute(
        """INSERT INTO paper_fills(time_ms, agent, coin, side, px, sz,
                                   closed_pnl, fee, cloid, reasoning)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, agent, coin, side, px, sz, closed_pnl, fee, cloid, reasoning),
    )


def _log_audit(d: Decision, conn: sqlite3.Connection, *, action: str, px: float, sz: float) -> None:
    log_decision(conn, Decision(
        agent=d.agent, action=action, coin=d.coin, side=d.side,  # type: ignore[arg-type]
        sz=sz, px=px, cloid=d.cloid,
        reasoning=f"[paper-sim] {d.reasoning or ''}".strip(),
        market_snapshot=d.market_snapshot, is_paper=True,
    ))


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


def simulate_cycle(
    conn: sqlite3.Connection,
    view: MarketView,
    decisions: list[Decision],
    *,
    cost: CostModel | None = None,
    maker_entries: bool = False,
    now_ms: int | None = None,
) -> PaperCycleResult:
    """Run one paper execution cycle: reconcile resting maker orders, accrue
    funding on open positions, then execute this cycle's place/flatten
    decisions. ``decisions`` must already be filtered to paper-mode agents."""
    cost = cost or CostModel()
    ts = now_ms or view.ts_ms or int(time.time() * 1000)
    res = PaperCycleResult()

    _reconcile_resting(conn, view, cost, ts, res)
    res.funding_rows = _accrue_funding(conn, view, ts)

    for d in decisions:
        if d.coin is None or d.action not in ("place", "flatten"):
            continue
        mid = view.mids.get(d.coin)
        if mid is None or mid <= 0:
            continue
        if d.action == "place" and d.sz and d.side in ("B", "A"):
            if maker_entries:
                limit_px = d.px or mid
                # One resting quote per (agent, coin) — agents can't see "rest"
                # audit rows as ownership, so they re-emit the same entry every
                # cycle; without this the quotes stack and all fill on a cross.
                # Replacing (rather than skipping) mirrors live maker repricing.
                conn.execute(
                    "DELETE FROM paper_orders WHERE agent = ? AND coin = ?",
                    (d.agent, d.coin),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO paper_orders
                       (cloid, agent, coin, side, sz, limit_px, created_ms, reasoning)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (d.cloid or f"paper-{d.agent}-{d.coin}-{ts}", d.agent, d.coin,
                     d.side, float(d.sz), float(limit_px), ts, d.reasoning),
                )
                _log_audit(d, conn, action="rest", px=float(limit_px), sz=float(d.sz))
                res.rested.append((d.agent, d.coin, d.side, float(d.sz), float(limit_px)))
            else:
                slip = cost.slippage_bps / 10_000.0
                px = mid * (1 + slip) if d.side == "B" else mid * (1 - slip)
                _execute(conn, d, px=px, sz=float(d.sz), side=d.side,
                         fee_rate=cost.taker_fee_bps / 10_000.0, ts=ts, res=res)
        elif d.action == "flatten":
            pos = _positions(conn, d.agent).get(d.coin)
            if not pos:
                continue
            exit_side = "A" if pos["net_sz"] > 0 else "B"
            slip = cost.slippage_bps / 10_000.0
            px = mid * (1 - slip) if exit_side == "A" else mid * (1 + slip)
            _execute(conn, d, px=px, sz=abs(pos["net_sz"]), side=exit_side,
                     fee_rate=cost.taker_fee_bps / 10_000.0, ts=ts, res=res,
                     audit_action="flatten")
    conn.commit()
    return res


def _execute(
    conn: sqlite3.Connection, d: Decision, *, px: float, sz: float, side: str,
    fee_rate: float, ts: int, res: PaperCycleResult, audit_action: str = "place",
) -> None:
    pos = _positions(conn, d.agent).get(d.coin or "", None)
    closed = 0.0
    if pos is not None:
        prev = pos["net_sz"]
        signed = sz if side == "B" else -sz
        if prev * signed < 0:  # reduces / closes / flips
            reduced = min(abs(prev), sz)
            direction = 1.0 if prev > 0 else -1.0
            closed = (px - pos["avg_entry_px"]) * reduced * direction
    fee = abs(px * sz) * fee_rate
    _write_fill(conn, ts_ms=ts, agent=d.agent, coin=d.coin or "", side=side,
                px=px, sz=sz, closed_pnl=closed, fee=fee,
                cloid=d.cloid, reasoning=d.reasoning)
    _log_audit(d, conn, action=audit_action, px=px, sz=sz)
    res.fills.append((d.agent, d.coin or "", side, sz, px))


def _reconcile_resting(
    conn: sqlite3.Connection, view: MarketView, cost: CostModel, ts: int,
    res: PaperCycleResult,
) -> None:
    """Fill resting paper maker orders whose limit the price has CROSSED."""
    rows = conn.execute("SELECT * FROM paper_orders").fetchall()
    for r in rows:
        mid = view.mids.get(r["coin"])
        if mid is None or mid <= 0:
            continue
        crossed = mid < r["limit_px"] if r["side"] == "B" else mid > r["limit_px"]
        if not crossed:
            continue
        d = Decision(agent=r["agent"], action="place", coin=r["coin"], side=r["side"],
                     sz=float(r["sz"]), px=float(r["limit_px"]), cloid=r["cloid"],
                     reasoning=r["reasoning"], is_paper=True)
        _execute(conn, d, px=float(r["limit_px"]), sz=float(r["sz"]), side=r["side"],
                 fee_rate=cost.maker_fee_bps / 10_000.0, ts=ts, res=res)
        conn.execute("DELETE FROM paper_orders WHERE cloid = ?", (r["cloid"],))


def _accrue_funding(conn: sqlite3.Connection, view: MarketView, ts: int) -> int:
    """Accrue funding on open paper positions for the time elapsed since each
    position's last accrual. ``view.funding`` is the 1h rate; positive rate
    means longs pay shorts (sign convention matches HL userFunding usdc)."""
    n = 0
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT agent FROM paper_fills").fetchall()]
    for agent in agents:
        for coin, pos in _positions(conn, agent).items():
            rate = view.funding.get(coin)
            mid = view.mids.get(coin)
            if rate is None or mid is None or mid <= 0:
                continue
            last = conn.execute(
                "SELECT MAX(time_ms) FROM paper_funding WHERE agent=? AND coin=?",
                (agent, coin),
            ).fetchone()[0]
            # Never accrue across a flat gap: a reopened position starts the
            # clock at its own opened_ms, not the previous position's last
            # funding row.
            since = max(int(last or 0), int(pos["opened_ms"]))
            hours = (ts - since) / 3_600_000.0
            if hours < 1.0:
                continue
            usdc = -pos["net_sz"] * mid * float(rate) * hours
            conn.execute(
                """INSERT OR REPLACE INTO paper_funding(time_ms, agent, coin, usdc)
                   VALUES(?,?,?,?)""",
                (ts, agent, coin, usdc),
            )
            n += 1
    return n
