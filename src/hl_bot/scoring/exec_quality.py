"""Execution-quality telemetry from the maker_orders lifecycle table.

Maker execution only beats taker if quotes actually fill. These metrics are
the feedback loop for MakerConfig tuning (backlog E2) and the health alerts
that catch a silent execution regression: a strategy can have real edge and
still bleed if its quotes never fill and everything escalates to taker.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field


@dataclass
class ExecQuality:
    agent: str
    window_h: float
    n_quotes: int = 0
    n_filled: int = 0
    n_partial: int = 0
    n_expired: int = 0
    n_fallback: int = 0
    fill_rate: float | None = None
    median_time_to_fill_s: float | None = None
    avg_reprices: float | None = None
    fallback_rate: float | None = None

    def row(self) -> str:
        fr = "—" if self.fill_rate is None else f"{self.fill_rate * 100:.0f}%"
        tt = "—" if self.median_time_to_fill_s is None else f"{self.median_time_to_fill_s:.0f}s"
        fb = "—" if self.fallback_rate is None else f"{self.fallback_rate * 100:.0f}%"
        rp = "—" if self.avg_reprices is None else f"{self.avg_reprices:.1f}"
        return (f"{self.agent:18s} quotes {self.n_quotes:3d} · fill {fr:>4s} "
                f"· t-fill {tt:>5s} · reprices {rp:>4s} · fallback {fb:>4s}")


@dataclass
class ExecQualityReport:
    per_agent: list[ExecQuality] = field(default_factory=list)

    def alerts(self, *, min_fill_rate: float = 0.30, max_fallback_rate: float = 0.25,
               min_quotes: int = 5) -> list[str]:
        out = []
        for q in self.per_agent:
            if q.n_quotes < min_quotes:
                continue
            if q.fill_rate is not None and q.fill_rate < min_fill_rate:
                out.append(f"{q.agent}: maker fill rate {q.fill_rate*100:.0f}% < "
                           f"{min_fill_rate*100:.0f}% over {q.window_h:g}h")
            if q.fallback_rate is not None and q.fallback_rate > max_fallback_rate:
                out.append(f"{q.agent}: taker-fallback rate {q.fallback_rate*100:.0f}% > "
                           f"{max_fallback_rate*100:.0f}%")
        return out


def exec_quality(
    conn: sqlite3.Connection, *, window_h: float = 24.0, now_ms: int | None = None,
) -> ExecQualityReport:
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - int(window_h * 3_600_000)
    try:
        rows = conn.execute(
            """SELECT agent, state, created_ms, updated_ms, reprice_count, parent_cloid
               FROM maker_orders WHERE created_ms >= ?""",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ExecQualityReport()

    by_agent: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_agent.setdefault(r["agent"], []).append(r)

    report = ExecQualityReport()
    for agent, rs in sorted(by_agent.items()):
        # A reprice chain is ONE economic quote: count only chain roots, but
        # credit the chain a fill if any link filled.
        roots = [r for r in rs if r["parent_cloid"] is None]
        n_quotes = len(roots) or len(rs)
        terminal = [r["state"] for r in rs]
        filled = [r for r in rs if r["state"] == "filled"]
        q = ExecQuality(agent=agent, window_h=window_h, n_quotes=n_quotes)
        q.n_filled = len(filled)
        q.n_partial = terminal.count("partial")
        q.n_expired = terminal.count("expired")
        q.n_fallback = terminal.count("taker_fallback")
        if n_quotes:
            q.fill_rate = q.n_filled / n_quotes
            q.fallback_rate = q.n_fallback / n_quotes
            q.avg_reprices = sum(int(r["reprice_count"]) for r in rs) / n_quotes
        ttf = sorted((int(r["updated_ms"]) - int(r["created_ms"])) / 1000.0
                     for r in filled)
        if ttf:
            q.median_time_to_fill_s = ttf[len(ttf) // 2]
        report.per_agent.append(q)
    return report
