"""Per-agent position replay + funding attribution (backlog B9 + B6).

The exchange pays funding on the ACCOUNT's net position; agents are logical.
To score a funding strategy honestly we must know which agent held what when
each payment landed. ``replay_positions`` rebuilds per-(agent, coin) state from
the fills audit trail; ``attribute_funding`` prorates every funding payment by
the agents' signed net sizes at payment time, with any remainder credited to
the ``_account`` residual row so per-agent totals always reconcile to the
exchange truth.
"""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict

RESIDUAL_AGENT = "_account"


def _signed(side: str, sz: float) -> float:
    return sz if side == "B" else -sz


def replay_positions(conn: sqlite3.Connection) -> int:
    """Rebuild the ``positions`` table from fills. Returns rows written.

    Walks all fills in time order maintaining per-(agent, coin) net size,
    average entry, realized PnL and fees. Survives partial fills, flips and
    manual interference because it derives purely from exchange fills.
    """
    rows = conn.execute(
        """SELECT agent, coin, side, px, sz, closed_pnl, fee, time_ms
           FROM fills WHERE agent IS NOT NULL
           ORDER BY time_ms ASC, tid ASC"""
    ).fetchall()

    state: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["agent"], r["coin"])
        st = state.setdefault(key, {
            "net_sz": 0.0, "avg_entry_px": 0.0,
            "realized_pnl": 0.0, "fees_paid": 0.0, "last_update_ms": 0,
        })
        prev = st["net_sz"]
        delta = _signed(r["side"], float(r["sz"]))
        new = prev + delta
        if prev == 0 or prev * delta > 0:
            # opening or adding: volume-weighted average entry
            total = abs(prev) + abs(delta)
            if total > 0:
                st["avg_entry_px"] = (
                    st["avg_entry_px"] * abs(prev) + float(r["px"]) * abs(delta)
                ) / total
        elif new != 0 and prev * new < 0:
            # flipped through zero: remaining size is a fresh entry at this px
            st["avg_entry_px"] = float(r["px"])
        elif new == 0:
            st["avg_entry_px"] = 0.0
        st["net_sz"] = new
        st["realized_pnl"] += float(r["closed_pnl"] or 0.0)
        st["fees_paid"] += float(r["fee"] or 0.0)
        st["last_update_ms"] = int(r["time_ms"])

    conn.execute("DELETE FROM positions")
    n = 0
    for (agent, coin), st in state.items():
        conn.execute(
            """INSERT INTO positions(agent, coin, net_sz, avg_entry_px,
                                     realized_pnl, fees_paid, last_update_ms)
               VALUES(?,?,?,?,?,?,?)""",
            (agent, coin, st["net_sz"], st["avg_entry_px"],
             st["realized_pnl"], st["fees_paid"], st["last_update_ms"]),
        )
        n += 1
    conn.commit()
    return n


def position_timeline(
    conn: sqlite3.Connection, coin: str
) -> tuple[list[int], list[dict[str, float]]]:
    """Per-agent signed net size on ``coin`` after each fill.

    Returns parallel lists (timestamps, snapshots) suitable for bisecting to
    answer "who held what at time T".
    """
    rows = conn.execute(
        """SELECT agent, side, sz, time_ms FROM fills
           WHERE coin = ? AND agent IS NOT NULL
           ORDER BY time_ms ASC, tid ASC""",
        (coin,),
    ).fetchall()
    times: list[int] = []
    snaps: list[dict[str, float]] = []
    net: dict[str, float] = defaultdict(float)
    for r in rows:
        net[r["agent"]] += _signed(r["side"], float(r["sz"]))
        times.append(int(r["time_ms"]))
        snaps.append({a: s for a, s in net.items() if abs(s) > 1e-12})
    return times, snaps


def _sizes_at(times: list[int], snaps: list[dict[str, float]], t_ms: int) -> dict[str, float]:
    i = bisect.bisect_right(times, t_ms) - 1
    return snaps[i] if i >= 0 else {}


def attribute_funding(conn: sqlite3.Connection) -> int:
    """Recompute the full ``funding_attribution`` table. Returns rows written.

    For each funding payment, each agent's share is ``usdc * net_a / sum(net)``
    over the agents' signed sizes at payment time (an agent positioned against
    the account's net direction gets an opposite-sign share). Whatever cannot
    be attributed — no agent positions on record, or sizes that don't sum to
    the exchange's — lands on the ``_account`` residual row, so
    ``SUM(funding_attribution.usdc)`` always equals ``SUM(funding_payments.usdc)``.

    Full recompute by design: idempotent and self-healing after backfills.
    """
    payments = conn.execute(
        "SELECT time_ms, coin, usdc, szi FROM funding_payments ORDER BY coin, time_ms"
    ).fetchall()

    timelines: dict[str, tuple[list[int], list[dict[str, float]]]] = {}
    out: list[tuple[int, str, str, float]] = []
    for p in payments:
        coin = p["coin"]
        usdc = float(p["usdc"] or 0.0)
        if coin not in timelines:
            timelines[coin] = position_timeline(conn, coin)
        sizes = _sizes_at(*timelines[coin], int(p["time_ms"]))
        # Funding is proportional to the signed position; prorate by the
        # account net implied by our agents. szi (exchange position at payment)
        # is preferred as the denominator when it's available and non-zero so a
        # manual position alongside the bots dilutes the bots' share correctly.
        denom = float(p["szi"]) if p["szi"] not in (None, 0) else sum(sizes.values())
        attributed = 0.0
        if sizes and abs(denom) > 1e-12:
            for agent, net in sizes.items():
                share = usdc * (net / denom)
                if abs(share) > 1e-12:
                    out.append((int(p["time_ms"]), coin, agent, share))
                    attributed += share
        residual = usdc - attributed
        if abs(residual) > 1e-9 or not sizes:
            out.append((int(p["time_ms"]), coin, RESIDUAL_AGENT, residual))

    conn.execute("DELETE FROM funding_attribution")
    conn.executemany(
        "INSERT OR REPLACE INTO funding_attribution(time_ms, coin, agent, usdc) VALUES(?,?,?,?)",
        out,
    )
    conn.commit()
    return len(out)


def refresh_attribution(conn: sqlite3.Connection) -> tuple[int, int]:
    """Run the full measurement refresh after ingest: positions + funding."""
    return replay_positions(conn), attribute_funding(conn)


def peak_gross_notional(
    conn: sqlite3.Connection, agent: str, since_ms: int | None,
    *, table: str = "fills",
) -> float:
    """Peak open notional (sum over coins of |net_sz| * px at fill time) the
    agent carried in the window — the per-agent 'capital at risk' base used to
    turn its synthetic PnL curve into relative Sharpe/drawdown numbers."""
    order = "time_ms ASC, tid ASC" if table == "fills" else "time_ms ASC, id ASC"
    q = f"""SELECT coin, side, px, sz, time_ms FROM {table}
            WHERE agent = ? ORDER BY {order}"""
    rows = conn.execute(q, (agent,)).fetchall()
    net: dict[str, float] = defaultdict(float)
    last_px: dict[str, float] = {}
    peak = 0.0
    for r in rows:
        net[r["coin"]] += _signed(r["side"], float(r["sz"]))
        last_px[r["coin"]] = float(r["px"])
        if since_ms is not None and int(r["time_ms"]) < since_ms:
            continue
        gross = sum(abs(net[c]) * last_px[c] for c in net)
        peak = max(peak, gross)
    return peak
