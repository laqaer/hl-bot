"""VetoAgent: blocks trading in coins where YOUR (account-level) edge is negative.

Rationale: Guda's manual scorecard shows certain coins (ZEC, HYPE, SOL) bleeding
net PnL over 30d while BTC carries the book. A veto agent doesn't generate
alpha — it removes negative-edge trades from the consideration set.

How it works each tick:
  1. Compute realized PnL per coin over a configurable lookback (default 30d)
     from the `fills` table (all attribution sources combined).
  2. For each coin in the watch list, emit a `block`/`allow` advisory Decision
     with the per-coin edge in bps.
  3. Other agents (or you, manually) can consult the latest VetoAgent decision
     for a coin before placing — but VetoAgent never places orders itself.

The decision rows show up in scorecards under agent=`veto_v1` with 0 PnL —
they're observations, not trades.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .base import Agent, MarketView
from .decisions import Decision


class VetoAgent(Agent):
    """Advisory agent. Emits block/allow decisions per coin based on rolling edge."""

    def __init__(
        self,
        name: str = "veto_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config, conn)
        self.lookback_days = int(self.config.get("lookback_days", 30))
        # Minimum trades required before a coin can be vetoed (avoid small-N noise)
        self.min_trades = int(self.config.get("min_trades", 20))
        # Edge threshold in bps. More negative = stricter veto.
        self.veto_threshold_bps = float(self.config.get("veto_threshold_bps", -5.0))
        self.conn = conn

    def _per_coin_edge_bps(self) -> dict[str, dict[str, float]]:
        """Return {coin: {edge_bps, n_trades, net_pnl}} over lookback window."""
        if self.conn is None:
            return {}
        since_ms = int((time.time() - self.lookback_days * 86_400) * 1000)
        rows = self.conn.execute(
            """
            SELECT coin,
                   COUNT(*) AS n_trades,
                   COALESCE(SUM(closed_pnl), 0) AS realized,
                   COALESCE(SUM(fee), 0) AS fees,
                   COALESCE(SUM(ABS(px * sz)), 0) AS notional
            FROM fills
            WHERE time_ms >= ? AND coin IS NOT NULL
            GROUP BY coin
            """,
            (since_ms,),
        ).fetchall()
        out: dict[str, dict[str, float]] = {}
        for r in rows:
            coin = r["coin"] if hasattr(r, "keys") else r[0]
            n = int(r["n_trades"] if hasattr(r, "keys") else r[1])
            realized = float(r["realized"] if hasattr(r, "keys") else r[2])
            fees = float(r["fees"] if hasattr(r, "keys") else r[3])
            notional = float(r["notional"] if hasattr(r, "keys") else r[4])
            net = realized - fees
            edge_bps = (net / notional * 10_000) if notional > 0 else 0.0
            out[coin] = {
                "n_trades": n,
                "net_pnl": net,
                "notional": notional,
                "edge_bps": edge_bps,
            }
        return out

    def decide(self, view: MarketView) -> list[Decision]:
        edges = self._per_coin_edge_bps()
        out: list[Decision] = []
        # Emit one decision per coin we have an opinion on (mids present + history exists)
        coins = set(view.mids.keys()) | set(edges.keys())
        for coin in sorted(coins):
            e = edges.get(coin, {})
            n = int(e.get("n_trades", 0))
            edge_bps = float(e.get("edge_bps", 0.0))
            net_pnl = float(e.get("net_pnl", 0.0))

            if n < self.min_trades:
                action = "hold"
                reason = f"{coin}: insufficient history ({n} trades < {self.min_trades})"
                verdict = "no-opinion"
            elif edge_bps < self.veto_threshold_bps:
                action = "hold"
                reason = (
                    f"{coin}: VETO — {self.lookback_days}d edge {edge_bps:+.1f} bps "
                    f"on {n} trades, net {net_pnl:+.2f}"
                )
                verdict = "veto"
            else:
                action = "hold"
                reason = (
                    f"{coin}: ALLOW — {self.lookback_days}d edge {edge_bps:+.1f} bps "
                    f"on {n} trades, net {net_pnl:+.2f}"
                )
                verdict = "allow"

            out.append(Decision(
                agent=self.name,
                action=action,  # advisory: always 'hold' since this agent never places
                coin=coin,
                reasoning=reason,
                market_snapshot={
                    "verdict": verdict,
                    "edge_bps": edge_bps,
                    "n_trades": n,
                    "net_pnl": net_pnl,
                    "lookback_days": self.lookback_days,
                    "mid": view.mids.get(coin),
                },
            ))
        return out


def current_vetoes(conn: sqlite3.Connection, max_age_min: int = 30) -> set[str]:
    """Lookup which coins are currently vetoed by the most recent veto_v1 decision.

    Returns a set of coin tickers under veto. Use this from other agents:

        vetoed = current_vetoes(conn)
        if coin in vetoed: skip

    Only considers decisions from the last `max_age_min` minutes (default 30).
    """
    cutoff_ms = int((time.time() - max_age_min * 60) * 1000)
    rows = conn.execute(
        """
        SELECT coin, market_snapshot
        FROM agent_decisions
        WHERE agent = 'veto_v1' AND ts_ms >= ?
        ORDER BY ts_ms DESC
        """,
        (cutoff_ms,),
    ).fetchall()
    seen: set[str] = set()
    vetoed: set[str] = set()
    for r in rows:
        coin = r["coin"] if hasattr(r, "keys") else r[0]
        snap_raw = r["market_snapshot"] if hasattr(r, "keys") else r[1]
        if not coin or coin in seen:
            continue
        seen.add(coin)
        try:
            import json
            snap = json.loads(snap_raw) if snap_raw else {}
        except (ValueError, TypeError):
            continue
        if snap.get("verdict") == "veto":
            vetoed.add(coin)
    return vetoed
