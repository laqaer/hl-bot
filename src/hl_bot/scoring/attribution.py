"""Per-agent attribution from ground-truth fills — funding + positions replay.

Fixes the two honest-measurement gaps called out in docs/REVIEW.md:

* **C4 — funding attribution.** ``funding_payments`` rows are account-level;
  scorecards previously credited funding only to the synthetic ``_account``
  agent, so a *funding* strategy's main revenue line was invisible to its own
  scorecard. Here we replay each agent's fills into a signed position timeline
  per coin and attribute each funding payment to whoever held that coin when it
  was paid (proportional by |size| if several agents held legs).
* **M2 — the unused ``positions`` table.** ``replay_positions_table`` rebuilds
  per-(agent, coin) net size / average entry / realized PnL / fees from fills,
  so attribution survives partial fills and manual interference.

Everything derives from the ``fills`` table (exchange truth, cloid-attributed at
ingest); nothing here invents PnL.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right

# Pseudo-agents that must never receive funding attribution as if they were
# strategies. "manual" *does* participate (a human position collects funding).
_EXCLUDED = ("_account",)


# Single-slot timeline cache. Rebuilding timelines scans the whole fills
# table; score_all/track_record would otherwise do that once per agent per
# window. conn.total_changes ticks on every row this connection writes (and
# the app is single-connection per process), so it is a sound invalidation
# stamp for reads.
_TIMELINE_CACHE: tuple[int, int, dict] | None = None


def position_timelines(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, tuple[list[int], list[float]]]]:
    """coin -> agent -> (fill_times_ms, signed_net_size_after_each_fill).

    Built from ALL fills (not window-filtered): a position opened before a
    scoring window still collects funding inside it. Buys add size, sells
    subtract; the running sum is the net position the agent's fills imply.
    Callers must treat the returned (cached) structure as read-only.
    """
    global _TIMELINE_CACHE
    if _TIMELINE_CACHE is not None:
        cid, stamp, cached = _TIMELINE_CACHE
        if cid == id(conn) and stamp == conn.total_changes:
            return cached
    rows = conn.execute(
        """SELECT time_ms, coin, side, sz, agent FROM fills
           WHERE agent IS NOT NULL ORDER BY time_ms ASC"""
    ).fetchall()
    out: dict[str, dict[str, tuple[list[int], list[float]]]] = {}
    for r in rows:
        agent = r["agent"]
        if agent in _EXCLUDED:
            continue
        delta = float(r["sz"] or 0) * (1.0 if r["side"] == "B" else -1.0)
        times, sizes = out.setdefault(r["coin"], {}).setdefault(agent, ([], []))
        prev = sizes[-1] if sizes else 0.0
        times.append(int(r["time_ms"]))
        sizes.append(prev + delta)
    _TIMELINE_CACHE = (id(conn), conn.total_changes, out)
    return out


def _held_at(timeline: tuple[list[int], list[float]], ts_ms: int) -> float:
    """Signed net size implied by fills strictly before ``ts_ms``."""
    times, sizes = timeline
    i = bisect_right(times, ts_ms - 1)
    return sizes[i - 1] if i > 0 else 0.0


def _funding_splits(
    conn: sqlite3.Connection,
    since_ms: int | None,
    timelines: dict[str, dict[str, tuple[list[int], list[float]]]],
):
    """Yield (time_ms, agent, usdc_share) for every funding payment.

    The single source of truth for the attribution rule. The exchange pays
    funding on the account's NET position: ``usdc = -net_szi * px * rate``. So
    each agent's true share is **signed**: ``usdc * signed_i / net_signed`` —
    an agent short while another is long receives funding with the opposite
    sign of the payer (and shares always sum to the account row exactly).
    A |size|-proportional split would mis-sign the hedged side, corrupting
    precisely the carry agents whose whole edge is collecting funding.

    Rows with no replayed position (e.g. predating fill history) or a ~zero
    net (internally hedged book: the exchange row is ~0 anyway and the
    denominator is unstable) yield nothing — per-agent numbers never
    overclaim, and the account-level total stays exact via ``_account``'s
    direct sum.
    """
    q = "SELECT time_ms, coin, usdc FROM funding_payments"
    params: list = []
    if since_ms is not None:
        q += " WHERE time_ms >= ?"
        params.append(since_ms)
    for r in conn.execute(q, params):
        by_agent = timelines.get(r["coin"])
        if not by_agent:
            continue
        ts = int(r["time_ms"])
        held = {a: _held_at(tl, ts) for a, tl in by_agent.items()}
        net = sum(held.values())
        if abs(net) <= 1e-9:
            continue
        usdc = float(r["usdc"] or 0)
        for agent, sz in held.items():
            if abs(sz) > 1e-12:
                yield ts, agent, usdc * sz / net


def attribute_funding(
    conn: sqlite3.Connection,
    since_ms: int | None = None,
    *,
    timelines: dict[str, dict[str, tuple[list[int], list[float]]]] | None = None,
) -> dict[str, float]:
    """agent -> attributed funding USDC over the window (+received, -paid)."""
    timelines = timelines if timelines is not None else position_timelines(conn)
    out: dict[str, float] = {}
    for _, agent, share in _funding_splits(conn, since_ms, timelines):
        out[agent] = out.get(agent, 0.0) + share
    return out


def funding_events_for_agent(
    conn: sqlite3.Connection,
    agent: str,
    since_ms: int | None = None,
    *,
    timelines: dict[str, dict[str, tuple[list[int], list[float]]]] | None = None,
) -> list[tuple[int, float]]:
    """(time_ms, usdc_share) funding events attributed to ``agent`` — the
    inputs an agent's equity curve needs alongside its fills."""
    timelines = timelines if timelines is not None else position_timelines(conn)
    return [
        (ts, share)
        for ts, a, share in _funding_splits(conn, since_ms, timelines)
        if a == agent
    ]


def agent_pnl_events(
    conn: sqlite3.Connection,
    agent: str,
    since_ms: int | None = None,
    *,
    funding_events: list[tuple[int, float]] | None = None,
) -> list[tuple[int, float]]:
    """Merged (time_ms, net_pnl_delta) events for one agent: fill PnL net of
    fees plus attributed funding. Cumulative sum of these is the agent's
    equity curve (baseline 0 before the first event).

    Pass ``funding_events`` when the caller already computed them (e.g.
    ``score_agent`` needs the funding sum anyway) to avoid replaying the
    fills timeline twice.
    """
    q = (
        "SELECT time_ms, COALESCE(closed_pnl,0) - COALESCE(fee,0) AS net "
        "FROM fills WHERE agent = ?"
    )
    params: list = [agent]
    if since_ms is not None:
        q += " AND time_ms >= ?"
        params.append(since_ms)
    events = [(int(r["time_ms"]), float(r["net"])) for r in conn.execute(q, params)]
    events.extend(
        funding_events
        if funding_events is not None
        else funding_events_for_agent(conn, agent, since_ms)
    )
    events.sort(key=lambda e: e[0])
    return events


def daily_pnl_series(events: list[tuple[int, float]]) -> list[float]:
    """Bucket PnL events into contiguous UTC days (zero-filled gaps)."""
    if not events:
        return []
    buckets: dict[int, float] = {}
    for ts_ms, delta in events:
        day = ts_ms // 86_400_000
        buckets[day] = buckets.get(day, 0.0) + delta
    lo, hi = min(buckets), max(buckets)
    return [buckets.get(d, 0.0) for d in range(lo, hi + 1)]


def replay_positions_table(conn: sqlite3.Connection) -> int:
    """Rebuild the ``positions`` table from fills. Returns rows written.

    Same-direction fills average into the entry; reducing fills keep the entry
    and book realized PnL; a flip resets the entry to the flipping fill's price
    (mirrors the backtest engine's position book).
    """
    rows = conn.execute(
        """SELECT time_ms, coin, side, px, sz, closed_pnl, fee, agent FROM fills
           WHERE agent IS NOT NULL ORDER BY time_ms ASC"""
    ).fetchall()
    book: dict[tuple[str, str], dict] = {}
    for r in rows:
        agent = r["agent"]
        if agent in _EXCLUDED:
            continue
        key = (agent, r["coin"])
        p = book.setdefault(key, {
            "net_sz": 0.0, "avg_entry_px": 0.0,
            "realized_pnl": 0.0, "fees_paid": 0.0, "last_update_ms": 0,
        })
        sz = float(r["sz"] or 0)
        px = float(r["px"] or 0)
        delta = sz if r["side"] == "B" else -sz
        net, avg = p["net_sz"], p["avg_entry_px"]
        new_net = net + delta
        if net == 0 or (net > 0) == (delta > 0):
            # opening or adding: average in
            tot = abs(net) + abs(delta)
            p["avg_entry_px"] = (avg * abs(net) + px * abs(delta)) / tot if tot else px
        elif (net > 0) != (new_net > 0) and abs(new_net) > 1e-12:
            # flipped through zero: remainder opens at this fill's price
            p["avg_entry_px"] = px
        elif abs(new_net) <= 1e-12:
            p["avg_entry_px"] = 0.0
        # reducing without flip keeps avg_entry_px
        p["net_sz"] = 0.0 if abs(new_net) <= 1e-12 else new_net
        p["realized_pnl"] += float(r["closed_pnl"] or 0)
        p["fees_paid"] += float(r["fee"] or 0)
        p["last_update_ms"] = int(r["time_ms"])

    cur = conn.cursor()
    cur.execute("DELETE FROM positions")
    for (agent, coin), p in book.items():
        cur.execute(
            """INSERT OR REPLACE INTO positions(
                 agent, coin, net_sz, avg_entry_px, realized_pnl, fees_paid, last_update_ms)
               VALUES(?,?,?,?,?,?,?)""",
            (agent, coin, p["net_sz"], p["avg_entry_px"],
             p["realized_pnl"], p["fees_paid"], p["last_update_ms"]),
        )
    conn.commit()
    return len(book)
