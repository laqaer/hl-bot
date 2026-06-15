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


def build_oi_change_view(
    conn: sqlite3.Connection,
    view,
    *,
    now_ms: int | None = None,
    lookback_s: float = 1800.0,
) -> dict[str, float]:
    """Live OI-crowding signal for S8 (``oi_crowding_reversal``): fractional
    open-interest growth over the last ``lookback_s`` per coin.

    ``oi_change = (oi_now - oi_ref) / oi_ref`` where ``oi_ref`` is the most recent
    ``market_samples`` OI at/just before the lookback horizon. OI rising fast =
    new positions piling in = the crowding the agent fades. This is computable
    ONLY from forward-accrued OI (candles carry none), which is the whole point
    of accruing it. Writes ``view.extra['oi_change']`` and returns the map. Must
    run AFTER ``accrue_market_samples`` (so this cycle's OI is the latest row)."""
    now_ms = now_ms or _now_ms()
    ref_floor = now_ms - int(lookback_s * 1000)
    oi_now_map = view.open_interest or {}
    out: dict[str, float] = {}
    for coin, oi_now in oi_now_map.items():
        if not _is_perp(coin) or oi_now is None or oi_now <= 0:
            continue
        row = conn.execute(
            """SELECT open_interest FROM market_samples
               WHERE coin=? AND ts_ms<=? AND open_interest IS NOT NULL
               ORDER BY ts_ms DESC LIMIT 1""",
            (coin, ref_floor),
        ).fetchone()
        if row is None or row[0] is None or float(row[0]) <= 0:
            continue
        oi_ref = float(row[0])
        out[coin] = (float(oi_now) - oi_ref) / oi_ref
    view.extra["oi_change"] = out
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


def accrue_frame_samples(
    conn: sqlite3.Connection,
    view,
    *,
    now_ms: int | None = None,
    intervals: tuple[str, ...] = ("5m", "1h"),
) -> int:
    """Persist the per-bar signal the engine already computes (vwap/sigma/mid/
    funding/vol) into the forward frame store, floored to the bar boundary.

    This is the linchpin of the forward flywheel: HL serves only ~5000
    candles/interval, so without it a 5m agent's confirm OOS can never grow past
    ~17.5d. With it, ``confirm`` rebuilds frames from ``accrued ∪ back-fetched``
    and the window grows forward. Reads ``view.extra['candles_<interval>']`` (the
    rolling vwap/sigma the live enrichment computes — the SAME basis the agent
    sees live), so the reconstructed frame matches live behaviour. Idempotent on
    the bar PK (first observation in a bar wins). Returns rows written."""
    now_ms = now_ms or _now_ms()
    fh = (view.extra.get("funding_hourly") or view.funding or {})
    vlm = (view.extra.get("day_ntl_vlm") or {})
    oic = (view.extra.get("oi_change") or {})
    written = 0
    for interval in intervals:
        bar_ms = _interval_ms(interval)
        if bar_ms <= 0:
            continue
        bar_ts = (now_ms // bar_ms) * bar_ms
        candles = (view.extra.get(f"candles_{interval}") or {})
        for coin, stats in candles.items():
            mid = (view.mids or {}).get(coin)
            vwap = (stats or {}).get("vwap")
            sigma = (stats or {}).get("sigma")
            if mid is None or mid <= 0 or vwap is None or sigma is None:
                continue
            written += conn.execute(
                """INSERT OR IGNORE INTO frame_samples(
                       interval, coin, bar_ts_ms, mid, funding_hourly, vwap, sigma,
                       vol, oi_change)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (interval, coin, bar_ts, float(mid), _f(fh.get(coin)),
                 float(vwap), float(sigma), _f(vlm.get(coin)), _f(oic.get(coin))),
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
    out = {"samples": 0, "listings": 0, "new_listings": 0, "frames": 0, "oi_change": 0}
    try:
        out["listings"] = accrue_listings(conn, view, now_ms=now_ms)
        out["samples"] = accrue_market_samples(
            conn, view, now_ms=now_ms, min_interval_s=sample_interval_s)
        # OI-change signal first (reads market_samples just written) so the frame
        # store persists it for S8's forward confirm.
        out["oi_change"] = len(build_oi_change_view(conn, view, now_ms=now_ms))
        out["frames"] = accrue_frame_samples(conn, view, now_ms=now_ms)
        nl = build_new_listings_view(
            conn, view, now_ms=now_ms,
            max_age_bars=listing_max_age_bars, bar_seconds=listing_bar_seconds)
        out["new_listings"] = len(nl)
    except sqlite3.Error:
        pass
    return out


_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _interval_ms(interval: str) -> int:
    return _INTERVAL_MS.get(interval, 0)


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
