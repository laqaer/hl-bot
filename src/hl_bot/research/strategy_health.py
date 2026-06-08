"""Measure strategy health from exchange-grounded fills and propose changes.

Everything here is read-only and risk-reducing by construction:
  * ``per_coin_contributions`` / ``agent_health`` summarize realized results
    across 24h/7d/30d windows, per-coin, including outlier concentration.
  * ``propose_overrides`` only ever TIGHTENS entries, REDUCES concurrency, or
    flags coins to veto. It never proposes raising any notional cap, and it
    flags outlier-dominated edges so one lucky coin (e.g. ZEC) can't make a
    bleeding agent look healthy.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

WINDOWS: dict[str, int] = {
    "24h": 86_400_000,
    "7d": 7 * 86_400_000,
    "30d": 30 * 86_400_000,
}


@dataclass
class CoinStat:
    coin: str
    n: int
    net: float
    notional: float
    edge_bps: float | None


@dataclass
class WindowStat:
    window: str
    n_trades: int
    net_pnl: float
    notional: float
    edge_bps: float | None


@dataclass
class AgentHealth:
    agent: str
    windows: dict[str, WindowStat]
    top_losers: list[CoinStat]
    top_winners: list[CoinStat]
    concentration: float | None
    losing_coins: list[str]
    # 7d realized edge EXCLUDING the single largest-net coin. This strips out a
    # lucky outlier (e.g. ZEC) so a bleeding book can't hide behind one winner.
    core_edge_bps: float | None = None


@dataclass
class Proposal:
    agent: str
    changes: dict[str, Any] = field(default_factory=dict)
    add_coin_vetoes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def per_coin_contributions(
    conn: sqlite3.Connection, agent: str, since_ms: int
) -> dict[str, CoinStat]:
    """Per-coin realized net (closed_pnl - fee) and traded notional for an agent."""
    rows = conn.execute(
        """SELECT coin,
                  COUNT(*) AS n,
                  COALESCE(SUM(closed_pnl), 0) - COALESCE(SUM(fee), 0) AS net,
                  COALESCE(SUM(ABS(px * sz)), 0) AS ntl
           FROM fills
           WHERE agent = ? AND time_ms >= ? AND coin IS NOT NULL
           GROUP BY coin""",
        (agent, since_ms),
    ).fetchall()
    out: dict[str, CoinStat] = {}
    for r in rows:
        ntl = float(r["ntl"] or 0.0)
        net = float(r["net"] or 0.0)
        out[r["coin"]] = CoinStat(
            coin=r["coin"],
            n=int(r["n"] or 0),
            net=net,
            notional=ntl,
            edge_bps=(net / ntl * 10_000) if ntl > 0 else None,
        )
    return out


def concentration_share(contrib: dict[str, CoinStat]) -> float | None:
    """Fraction of total POSITIVE net PnL contributed by the single best coin.

    Near 1.0 means the agent's profit is dominated by one outlier coin and the
    aggregate edge should not be trusted. None when nothing is positive.
    """
    positives = [c.net for c in contrib.values() if c.net > 0]
    total_pos = sum(positives)
    if total_pos <= 0:
        return None
    return max(positives) / total_pos


def _window_stat(
    conn: sqlite3.Connection, agent: str, window: str, now_ms: int
) -> WindowStat:
    since = now_ms - WINDOWS[window]
    contrib = per_coin_contributions(conn, agent, since)
    n = sum(c.n for c in contrib.values())
    net = sum(c.net for c in contrib.values())
    ntl = sum(c.notional for c in contrib.values())
    return WindowStat(
        window=window, n_trades=n, net_pnl=net, notional=ntl,
        edge_bps=(net / ntl * 10_000) if ntl > 0 else None,
    )


def edge_excluding_top(contrib: dict[str, CoinStat]) -> float | None:
    """Realized edge (bps) over all coins except the single largest-net coin."""
    items = list(contrib.values())
    if len(items) < 2:
        return None
    top = max(items, key=lambda c: c.net)
    rest = [c for c in items if c is not top]
    net = sum(c.net for c in rest)
    ntl = sum(c.notional for c in rest)
    return (net / ntl * 10_000) if ntl > 0 else None


def _losing_coins(
    contrib: dict[str, CoinStat],
    *,
    min_fills: int = 2,
    edge_bps_thresh: float = -25.0,
) -> list[str]:
    out = [
        c.coin for c in contrib.values()
        if c.n >= min_fills and c.net < 0
        and c.edge_bps is not None and c.edge_bps <= edge_bps_thresh
    ]
    return sorted(out)


def agent_health(
    conn: sqlite3.Connection, agent: str, now_ms: int | None = None
) -> AgentHealth:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    windows = {w: _window_stat(conn, agent, w, now_ms) for w in WINDOWS}

    contrib_30d = per_coin_contributions(conn, agent, now_ms - WINDOWS["30d"])
    contrib_7d = per_coin_contributions(conn, agent, now_ms - WINDOWS["7d"])
    ranked = sorted(contrib_30d.values(), key=lambda c: c.net)
    losers = [c for c in ranked if c.net < 0][:5]
    winners = [c for c in reversed(ranked) if c.net > 0][:5]

    return AgentHealth(
        agent=agent,
        windows=windows,
        top_losers=losers,
        top_winners=winners,
        concentration=concentration_share(contrib_30d),
        losing_coins=_losing_coins(contrib_7d),
        core_edge_bps=edge_excluding_top(contrib_7d),
    )


# ---------------------------------------------------------------------------
# Proposals (risk-reducing only)
# ---------------------------------------------------------------------------


def propose_overrides(
    healths: list[AgentHealth],
    current_params: dict[str, dict[str, Any]],
    *,
    bleeding_edge_bps: float = -10.0,
    min_trades: int = 15,
    concentration_warn: float = 0.6,
    sigma_step: float = 0.5,
    sigma_max: float = 4.0,
    concurrency_floor: int = 1,
) -> list[Proposal]:
    """Propose ONLY risk-reducing changes from measured health.

    Rules:
      * If an agent is bleeding over 7d (enough trades + edge below threshold):
        tighten its entry (raise sigma_enter), reduce concurrency by one, and
        list its loss-bleeding coins for veto.
      * If its edge is outlier-dominated, flag it (block promotion judgments).
      * Never propose increasing any notional cap.
    """
    proposals: list[Proposal] = []
    for h in healths:
        cur = current_params.get(h.agent, {})
        p = Proposal(agent=h.agent)

        # Loss-bleeding coins are vetoed regardless of aggregate edge: an
        # outlier winner must not let proven loser coins keep being re-entered.
        if h.losing_coins:
            p.add_coin_vetoes = list(h.losing_coins)
            p.rationale.append("veto loss-bleeding coins: " + ", ".join(h.losing_coins))

        # Bleeding uses the outlier-stripped core edge when available so a single
        # lucky coin can't mask a losing book.
        w7 = h.windows.get("7d")
        edge_for_bleed = h.core_edge_bps if h.core_edge_bps is not None else (
            w7.edge_bps if w7 is not None else None
        )
        bleeding = (
            w7 is not None and w7.n_trades >= min_trades
            and edge_for_bleed is not None and edge_for_bleed < bleeding_edge_bps
        )
        if bleeding:
            p.rationale.append(
                f"7d core edge {edge_for_bleed:+.0f} bps over {w7.n_trades} trades "
                f"(net ${w7.net_pnl:+.2f}) — bleeding, tightening."
            )
            sig = cur.get("sigma_enter")
            if sig is not None:
                new_sig = min(float(sig) + sigma_step, sigma_max)
                if new_sig > float(sig):
                    p.changes["sigma_enter"] = round(new_sig, 3)
            mcp = cur.get("max_concurrent_positions")
            if mcp is not None and int(mcp) > concurrency_floor:
                p.changes["max_concurrent_positions"] = int(mcp) - 1

        if h.concentration is not None and h.concentration > concentration_warn:
            top = h.top_winners[0].coin if h.top_winners else "?"
            p.flags.append(
                f"outlier-dominated edge: {top} is {h.concentration*100:.0f}% of "
                "positive PnL — aggregate edge unreliable, do not promote on it."
            )

        # Safety invariant: never emit a notional-raising change.
        for forbidden in ("max_notional_per_trade", "max_total_notional"):
            p.changes.pop(forbidden, None)

        proposals.append(p)
    return proposals


def build_proposal_document(proposals: list[Proposal]) -> dict[str, Any]:
    """Serialize proposals into a writable, mergeable JSON document.

    The ``overrides`` block is directly mergeable into agent_overrides.json;
    ``_meta`` carries advisory vetoes/flags/rationale for human review.
    """
    overrides: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for p in proposals:
        if p.changes:
            overrides[p.agent] = p.changes
        if p.changes or p.add_coin_vetoes or p.flags or p.rationale:
            meta[p.agent] = {
                "add_coin_vetoes": p.add_coin_vetoes,
                "flags": p.flags,
                "rationale": p.rationale,
            }
    return {
        "generated_ms": int(time.time() * 1000),
        "note": "Risk-reducing proposals only. Review before merging into "
                "agent_overrides.json. Notional increases are never auto-proposed.",
        "overrides": overrides,
        "_meta": meta,
    }
