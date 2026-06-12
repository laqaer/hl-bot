"""MetaAllocator — split capital across agents based on rolling 7d Sharpe.

NOT an Agent (no `decide()`). A helper used by the tick CLI to set each
agent's `max_total_notional` cap before its turn.

Allocation policy (total = TOTAL_CAPITAL, default $300):
  1. For any agent with <MIN_TRADES fills in 7d -> minimum allocation MIN_ALLOC
     (cold start protection; let new agents prove themselves).
  2. For any agent with negative Sharpe -> floor NEG_FLOOR (still alive, can recover).
  3. Remaining capital distributed to positive-Sharpe agents proportional to
     Sharpe.
  4. Caps capped at MAX_ALLOC so no single agent eats everything.

Sharpe is computed from daily PnL (closed_pnl - fee) over the last 7d, using
day-of-the-week buckets. Uses agent name prefix in fills.agent column.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class MetaAllocatorConfig:
    total_capital: float = 300.0
    min_alloc: float = 50.0          # new/cold agents
    neg_floor: float = 25.0          # negative-Sharpe agents
    max_alloc: float = 150.0         # ceiling per agent
    # 2026-06-12 audit: 7d annualized Sharpe from daily buckets is a noise
    # statistic that starves slow carry agents (1-2 fills/day = permanently
    # "cold") while Sharpe-weighting churny ones. 30d window + close-event
    # counting; single-agent share capped at 50%.
    min_trades: int = 10             # CLOSE events below this -> cold-start min_alloc
    window_days: int = 30
    max_share: float = 0.5


@dataclass
class AgentStats:
    agent: str
    n_trades: int = 0
    pnl_7d: float = 0.0
    sharpe: float | None = None
    daily_pnls: list[float] = field(default_factory=list)


class MetaAllocator:
    def __init__(self, agents: list[str], config: MetaAllocatorConfig | None = None) -> None:
        self.agents = list(agents)
        self.cfg = config or MetaAllocatorConfig()

    # ------------------------------------------------------------------
    def _agent_stats(self, conn: sqlite3.Connection, agent: str) -> AgentStats:
        cutoff_ms = int((time.time() - self.cfg.window_days * 86400) * 1000)
        rows = conn.execute(
            """SELECT time_ms, COALESCE(closed_pnl,0) - COALESCE(fee,0) AS pnl
               FROM fills WHERE agent = ? AND time_ms >= ?
               ORDER BY time_ms ASC""",
            (agent, cutoff_ms),
        ).fetchall()
        if not rows:
            return AgentStats(agent=agent)
        # Bucket by UTC day
        buckets: dict[int, float] = {}
        total = 0.0
        for r in rows:
            day = int(r["time_ms"] // 86_400_000)
            buckets[day] = buckets.get(day, 0.0) + float(r["pnl"])
            total += float(r["pnl"])
        # Fill missing days as 0 across the window
        today = int(time.time() // 86_400)
        daily = [buckets.get(today - i, 0.0) for i in range(self.cfg.window_days)]
        n = len(daily)
        mean = sum(daily) / n
        var = sum((x - mean) ** 2 for x in daily) / n
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / std * math.sqrt(365)) if std > 0 else None
        n_closes = sum(1 for r in rows if float(r["pnl"]) != 0.0)
        return AgentStats(
            agent=agent, n_trades=n_closes, pnl_7d=total,
            sharpe=sharpe, daily_pnls=daily,
        )

    # ------------------------------------------------------------------
    def allocate(self, conn: sqlite3.Connection) -> dict[str, float]:
        stats = {a: self._agent_stats(conn, a) for a in self.agents}
        allocs: dict[str, float] = {}
        # Step 1: classify
        cold = [a for a, s in stats.items() if s.n_trades < self.cfg.min_trades]
        warm = [a for a in self.agents if a not in cold]
        negative = [a for a in warm if (stats[a].sharpe or 0) <= 0]
        positive = [a for a in warm if (stats[a].sharpe or 0) > 0]

        # Step 2: assign floors
        for a in cold:
            allocs[a] = self.cfg.min_alloc
        for a in negative:
            allocs[a] = self.cfg.neg_floor

        used = sum(allocs.values())
        remaining = max(0.0, self.cfg.total_capital - used)

        # Step 3: positive Sharpe -> proportional split
        if positive:
            sum_sh = sum(stats[a].sharpe or 0 for a in positive)
            for a in positive:
                share = (stats[a].sharpe or 0) / sum_sh if sum_sh > 0 else 1.0 / len(positive)
                share = min(share, self.cfg.max_share)  # one agent never takes the book
                allocs[a] = min(self.cfg.max_alloc, max(self.cfg.min_alloc, remaining * share))
        else:
            # No positive performers — split remaining equally over warm-negatives
            if negative and remaining > 0:
                extra = remaining / len(negative)
                for a in negative:
                    allocs[a] = min(self.cfg.max_alloc, allocs[a] + extra)
            elif cold and remaining > 0:
                extra = remaining / len(cold)
                for a in cold:
                    allocs[a] = min(self.cfg.max_alloc, allocs[a] + extra)

        # Step 4: cap clamps
        for a in self.agents:
            allocs[a] = min(self.cfg.max_alloc, max(0.0, allocs.get(a, 0.0)))
        return allocs

    # ------------------------------------------------------------------
    def report(self, conn: sqlite3.Connection) -> list[dict]:
        """Human-readable per-agent stats + allocation."""
        allocs = self.allocate(conn)
        out = []
        for a in self.agents:
            s = self._agent_stats(conn, a)
            out.append({
                "agent": a,
                "n_trades_7d": s.n_trades,
                "pnl_7d": round(s.pnl_7d, 4),
                "sharpe": None if s.sharpe is None else round(s.sharpe, 3),
                "alloc_usd": round(allocs[a], 2),
            })
        return out
