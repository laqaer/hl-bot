"""Forward-evidence accrual (P1) — append-only signal capture each cycle.

HL `candleSnapshot` retains only ~5000 candles/interval, so low-frequency or
recent edges can never reach the G0 trade floor from back-fetched history. The
fix is to accrue the un-back-fetchable signals FORWARD, every engine cycle, into
append-only tables (`market_samples`, `xvenue_funding`, `listing_log`), and let
calendar time grow the sample until `hlbot confirm` clears the gate. See
`docs/research/P1_forward_evidence_flywheel.md`.

Pure given a MarketView + connection; the only state is the DB. Idempotent on
each table's PRIMARY KEY (INSERT OR IGNORE), so a double-call in one cycle, or a
replayed cycle, never double-counts.
"""

from __future__ import annotations

import sqlite3

HOURS_PER_YEAR = 24 * 365


def _is_perp(coin: str) -> bool:
    """Skip synthetic/derived mids the enrichment layer injects (e.g. spot legs
    surfaced as ``BTC-SPOT``) — we only sample real perp coins."""
    return "-" not in coin


def accrue_market_samples(
    conn: sqlite3.Connection,
    view,
    *,
    now_ms: int | None = None,
    min_interval_s: float = 60.0,
) -> int:
    """Append one row per perp coin (mid/funding/OI/vlm/book-imbalance).

    Throttled per coin to ``min_interval_s`` so a fast (~5s) engine cycle does
    not bloat the table — the forward study needs minute-resolution, not every
    tick. Returns rows written.
    """
    now_ms = now_ms or _now_ms()
    cutoff = now_ms - int(min_interval_s * 1000)
    funding = view.funding or {}
    oi = view.open_interest or {}
    vlm = (view.extra.get("day_ntl_vlm") or {})
    imb = (view.extra.get("book_imb") or {})
    written = 0
    for coin, mid in (view.mids or {}).items():
        if not _is_perp(coin) or mid is None or mid <= 0:
            continue
        last = conn.execute(
            "SELECT MAX(ts_ms) FROM market_samples WHERE coin=?", (coin,)
        ).fetchone()[0]
        if last is not None and int(last) > cutoff:
            continue  # sampled this coin too recently
        cur = conn.execute(
            """INSERT OR IGNORE INTO market_samples(
                   ts_ms, coin, mid, funding, open_interest, day_ntl_vlm, book_imb)
               VALUES(?,?,?,?,?,?,?)""",
            (now_ms, coin, float(mid), _f(funding.get(coin)), _f(oi.get(coin)),
             _f(vlm.get(coin)), _f(imb.get(coin))),
        )
        written += cur.rowcount
    return written


def accrue_listings(
    conn: sqlite3.Connection, view, *, now_ms: int | None = None,
) -> int:
    """Record first-seen + reference price for each perp coin.

    The first call on an empty table BACKFILLS the existing universe as
    ``source='backfill'`` — those coins predate our watching and must NOT be
    treated as new listings (otherwise the whole universe looks day-1 on
    deployment). Coins appearing AFTER that are genuine listings
    (``source='live'``). Returns rows written.
    """
    now_ms = now_ms or _now_ms()
    seeded = conn.execute("SELECT COUNT(*) FROM listing_log").fetchone()[0] > 0
    source = "live" if seeded else "backfill"
    known = {r[0] for r in conn.execute("SELECT coin FROM listing_log").fetchall()}
    written = 0
    for coin, mid in (view.mids or {}).items():
        if not _is_perp(coin) or coin in known or mid is None or mid <= 0:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO listing_log(coin, first_seen_ms, listing_px, source)
               VALUES(?,?,?,?)""",
            (coin, now_ms, float(mid), source),
        )
        written += cur.rowcount
    return written


def build_new_listings_view(
    conn: sqlite3.Connection,
    view,
    *,
    now_ms: int | None = None,
    max_age_bars: int = 24,
    bar_seconds: int = 3600,
) -> dict[str, dict]:
    """Live ``new_listings`` signal from the forward listing_log — the wiring the
    new_listing_reversion agent was missing (it only had the backtest path).

    Only ``source='live'`` coins (genuinely listed since we started watching)
    within ``max_age_bars`` of day-1 qualify; backfilled coins never do. The
    shape mirrors ``backtest/data.build_frames``: ``{age_bars, ref_px, vol_usd,
    recent_closes}``. Also sets ``view.extra['new_listings']`` for the agent.
    """
    now_ms = now_ms or _now_ms()
    max_age_ms = max_age_bars * bar_seconds * 1000
    vlm = (view.extra.get("day_ntl_vlm") or {})
    out: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT coin, first_seen_ms, listing_px FROM listing_log WHERE source='live'"
    ).fetchall()
    for coin, first_seen_ms, ref_px in rows:
        mid = (view.mids or {}).get(coin)
        if mid is None or mid <= 0 or not ref_px or ref_px <= 0:
            continue
        age_ms = now_ms - int(first_seen_ms)
        if age_ms < 0 or age_ms > max_age_ms:
            continue  # not within day 1 (or clock skew)
        out[coin] = {
            "age_bars": int(age_ms / (bar_seconds * 1000)),
            "ref_px": float(ref_px),
            "vol_usd": _f(vlm.get(coin)) or 0.0,
            "recent_closes": [float(ref_px), float(mid)],
        }
    view.extra["new_listings"] = out
    return out


def accrue_xvenue_funding(
    conn: sqlite3.Connection,
    xvenue: dict[str, dict[str, float]],
    *,
    hl_funding: dict[str, float] | None = None,
    now_ms: int | None = None,
) -> int:
    """Append cross-venue funding rows (S5 fuel). ``xvenue`` is the
    ``{coin: {'binance': per_hr, 'bybit': per_hr}}`` map from
    ``research.funding_xvenue``; ``hl_funding`` (per-hour) is stored as
    ``venue='hl'`` for the same timestamp so spreads are reconstructable.
    Per-hour rates are annualized to APR%. Returns rows written."""
    now_ms = now_ms or _now_ms()
    written = 0
    for coin, venues in (xvenue or {}).items():
        for venue, per_hr in (venues or {}).items():
            if per_hr is None:
                continue
            written += conn.execute(
                """INSERT OR IGNORE INTO xvenue_funding(ts_ms, coin, venue, funding_apr)
                   VALUES(?,?,?,?)""",
                (now_ms, coin, venue, float(per_hr) * HOURS_PER_YEAR * 100.0),
            ).rowcount
    for coin, per_hr in (hl_funding or {}).items():
        if per_hr is None:
            continue
        written += conn.execute(
            """INSERT OR IGNORE INTO xvenue_funding(ts_ms, coin, venue, funding_apr)
               VALUES(?,?,?,?)""",
            (now_ms, coin, "hl", float(per_hr) * HOURS_PER_YEAR * 100.0),
        ).rowcount
    return written


def accrue_cycle(
    conn: sqlite3.Connection,
    view,
    *,
    now_ms: int | None = None,
    sample_interval_s: float = 60.0,
    listing_max_age_bars: int = 24,
    listing_bar_seconds: int = 3600,
) -> dict[str, int]:
    """Run the per-cycle accrual (samples + listings) and wire the live
    ``new_listings`` signal into the view. The xvenue leg is host-only (geo-
    blocked from CI) and accrued by a separate nightly job. Best-effort: a
    failure in accrual must never break a trading cycle."""
    now_ms = now_ms or _now_ms()
    out = {"samples": 0, "listings": 0, "new_listings": 0}
    try:
        out["listings"] = accrue_listings(conn, view, now_ms=now_ms)
        out["samples"] = accrue_market_samples(
            conn, view, now_ms=now_ms, min_interval_s=sample_interval_s)
        nl = build_new_listings_view(
            conn, view, now_ms=now_ms,
            max_age_bars=listing_max_age_bars, bar_seconds=listing_bar_seconds)
        out["new_listings"] = len(nl)
    except sqlite3.Error:
        pass
    return out


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
