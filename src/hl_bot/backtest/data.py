"""Load Hyperliquid candle + funding history and assemble backtest frames.

Network fetch is isolated in small functions; the frame-assembly + rolling-stat
math is pure so it can be unit-tested without a network.

Hyperliquid info endpoints used (public, no auth):
  {"type": "candleSnapshot", "req": {"coin","interval","startTime","endTime"}}
  {"type": "fundingHistory", "coin", "startTime", "endTime"}
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from .engine import Frame

log = logging.getLogger(__name__)

# Bump when the on-disk frame schema changes so stale caches are recomputed.
# v2 adds spot mids (S4 spot-perp carry); v1 caches lacked them.
# v3 adds funding_hourly (unscaled 1h rate signal for funding_crowding_fade).
# v4 pages funding forward (was capped at the oldest 500 rows → recent funding
# was stale/missing, which is fatal for funding-as-signal agents).
# v5 adds the new-listing signal (per-frame age/ref-px/vol since listing) used by
# new_listing_reversion; a coin is "newly listed" when its first candle is
# materially later than the dataset's retention-cliff anchor.
CACHE_VERSION = 5

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 3_600_000,
    "4h": 4 * 3_600_000,
    "1d": 86_400_000,
}


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def _post_info(
    payload: dict[str, Any], *,
    base_url: str = "https://api.hyperliquid.xyz",
    timeout: float = 20.0, retries: int = 5,
) -> Any:
    """POST to the HL /info endpoint, retrying 429 (rate limit) with backoff.

    Multi-coin fetches that page funding fire many requests in a burst and HL
    rate-limits them; without backoff the whole load fails and a sweep silently
    reports 0 trades (a FALSE negative, not a strategy verdict). Backoff:
    0.5s,1,2,4,8s. Non-429 HTTP errors raise immediately.
    """
    delay = 0.5
    for attempt in range(retries + 1):
        with httpx.Client(timeout=timeout) as client:
            r = client.post(base_url + "/info", json=payload)
        if r.status_code == 429 and attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()  # exhausted retries on 429 -> surface it
    return r.json()


def fetch_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw candle dicts: {t, T, o, h, l, c, v, n}. Newest-last."""
    out = _post_info({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }, base_url=base_url)
    return out if isinstance(out, list) else []


_SPOT_META_CACHE: dict[str, Any] | None = None


def fetch_spot_meta(*, base_url: str = "https://api.hyperliquid.xyz") -> dict[str, Any]:
    """Fetch (and process-cache) the HL spot meta: {"universe": [...], "tokens": [...]}."""
    global _SPOT_META_CACHE
    if _SPOT_META_CACHE is not None:
        return _SPOT_META_CACHE
    out = _post_info({"type": "spotMeta"}, base_url=base_url)
    _SPOT_META_CACHE = out if isinstance(out, dict) else {"universe": [], "tokens": []}
    return _SPOT_META_CACHE


def spot_symbol_for(coin: str, spot_meta: dict[str, Any]) -> str | None:
    """Resolve a plain perp coin (e.g. "BTC") to its HL spot candle symbol.

    HL spot candle symbols are the spot *pair* name (universe entry "name", e.g.
    "@107" or "PURR/USDC"), NOT the plain coin. The base token is often wrapped
    with a "U" prefix (UBTC, UETH, USOL). We match the universe pair whose base
    token name equals ``coin`` or ``"U"+coin``, preferring a USDC-quoted pair,
    and return that pair's ``name``.

    Returns ``None`` if no pair resolves — the caller then simply skips the spot
    leg for that coin. The exact symbol format cannot be verified offline; the
    host run validates it (a failed fetch yields [] and the coin is skipped).
    """
    universe = spot_meta.get("universe", []) or []
    tokens = spot_meta.get("tokens", []) or []
    name_by_index = {t.get("index"): t.get("name") for t in tokens}
    wanted = {coin, f"U{coin}"}
    fallback: str | None = None
    for u in universe:
        pair_tokens = u.get("tokens", []) or []
        if not pair_tokens:
            continue
        base_name = name_by_index.get(pair_tokens[0])
        if base_name not in wanted:
            continue
        pair_name = u.get("name")
        if not pair_name:
            continue
        quote_name = name_by_index.get(pair_tokens[1]) if len(pair_tokens) > 1 else None
        if quote_name == "USDC":
            return str(pair_name)
        fallback = fallback or str(pair_name)
    return fallback


def fetch_spot_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Spot candles for ``coin`` resolved to its HL spot pair symbol.

    Returns [] (and logs a warning naming the coin + attempted symbol) if the
    coin has no resolvable spot pair or the snapshot comes back empty — the
    caller treats that as "no spot leg for this coin".
    """
    try:
        meta = fetch_spot_meta(base_url=base_url)
    except Exception as exc:  # noqa: BLE001 — network/format; skip the spot leg
        log.warning("spot meta fetch failed for %s: %s", coin, exc)
        return []
    symbol = spot_symbol_for(coin, meta)
    if not symbol:
        log.warning("no HL spot pair resolved for %s; skipping spot leg", coin)
        return []
    try:
        candles = fetch_candles(symbol, interval, start_ms, end_ms, base_url=base_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("spot candle fetch failed for %s (symbol %s): %s", coin, symbol, exc)
        return []
    if not candles:
        log.warning("empty spot candles for %s (tried symbol %s); skipping spot leg",
                    coin, symbol)
    return candles


def fetch_funding_history(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw funding rows: {coin, fundingRate, premium, time}. ONE request.

    HL caps this at ~500 rows anchored at ``startTime`` (the OLDEST 500 in the
    window), so a wide window returns only its oldest ~21 days — the recent end
    is missing. Callers wanting full coverage must page forward; see
    ``fetch_funding_history_window``.
    """
    out = _post_info({
        "type": "fundingHistory", "coin": coin,
        "startTime": start_ms, "endTime": end_ms,
    }, base_url=base_url)
    return out if isinstance(out, list) else []


_FUNDING_PAGE_CAP = 500  # HL's per-request fundingHistory row cap


def fetch_funding_history_window(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
    max_pages: int = 30,
) -> list[dict[str, Any]]:
    """Full funding history over [start, end], paging FORWARD past the 500-row cap.

    Each request returns the oldest ≤500 rows from its ``startTime``; we advance
    ``startTime`` to just past the newest row received and repeat until the
    window is covered (short page), no progress is made, or ``max_pages`` is hit.
    De-duplicates by timestamp. Without this, funding-as-signal agents (e.g.
    funding_crowding_fade) see only stale 69–90d-old rates on a wide window.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cur = start_ms
    for _ in range(max_pages):
        batch = fetch_funding_history(coin, cur, end_ms, base_url=base_url)
        if not batch:
            break
        fresh = [r for r in batch if int(r.get("time", 0)) not in seen]
        for r in fresh:
            seen.add(int(r.get("time", 0)))
        out.extend(fresh)
        last_t = max((int(r.get("time", 0)) for r in batch), default=cur)
        if len(batch) < _FUNDING_PAGE_CAP or last_t >= end_ms or last_t <= cur:
            break  # reached the end, or no forward progress
        cur = last_t + 1
    out.sort(key=lambda r: int(r.get("time", 0)))
    return out


# ---------------------------------------------------------------------------
# Pure transforms (unit-tested)
# ---------------------------------------------------------------------------


def _closes_vols(candles: list[dict[str, Any]]) -> tuple[list[float], list[float], list[int]]:
    closes, vols, ts = [], [], []
    for k in candles:
        try:
            c = float(k.get("c", 0))
            v = float(k.get("v", 0))
            t = int(k.get("t", 0))
        except (TypeError, ValueError):
            continue
        if c > 0:
            closes.append(c)
            vols.append(v)
            ts.append(t)
    return closes, vols, ts


def rolling_vwap_sigma(
    closes: list[float], vols: list[float], window: int
) -> tuple[float | None, float | None]:
    """VWAP and population stdev of the last ``window`` closes."""
    if len(closes) < max(2, window // 2):
        return None, None
    c = closes[-window:]
    v = vols[-window:]
    tot_v = sum(v)
    vwap = sum(p * w for p, w in zip(c, v, strict=False)) / tot_v if tot_v > 0 else sum(c) / len(c)
    mean = sum(c) / len(c)
    sigma = (sum((p - mean) ** 2 for p in c) / len(c)) ** 0.5
    return vwap, sigma


def funding_rate_at(funding_rows: list[dict[str, Any]], ts_ms: int) -> float:
    """Most recent funding rate at or before ``ts_ms`` (0.0 if none)."""
    best = 0.0
    for row in funding_rows:
        try:
            t = int(row.get("time", 0))
            rate = float(row.get("fundingRate", 0) or 0)
        except (TypeError, ValueError):
            continue
        if t <= ts_ms:
            best = rate
    return best


def frames_coverage_days(frames: list[Frame]) -> float:
    """Actual wall-clock span covered by ``frames``, in days (0.0 if <2 frames).

    Used to tell the truth about how much history a backtest *actually* saw:
    HL's ``candleSnapshot`` serves at most ~5000 candles per interval, so a
    fine interval cannot reach a long lookback regardless of the requested
    window (5m → ~17d, 15m → ~52d, 1h → ~208d). The requested ``days`` is then
    only a nominal upper bound, not the real evidence window.
    """
    if len(frames) < 2:
        return 0.0
    return (frames[-1].ts_ms - frames[0].ts_ms) / 86_400_000.0


def warn_if_short_coverage(frames: list[Frame], *, interval: str, days: int) -> float:
    """Log a WARNING when the built frames cover materially less than ``days``.

    Returns the actual coverage in days so callers can surface it in reports /
    confirmation records. The threshold (90% of requested) avoids noise from the
    one partial bar at each end while catching the retention cliff (e.g. a "90d"
    5m request that only yields ~17d). Never raises — purely advisory.
    """
    cov = frames_coverage_days(frames)
    if frames and days >= 2 and cov < days * 0.9:
        log.warning(
            "history coverage short: requested %dd of %s but only ~%.1fd of "
            "candles exist at HL (≤~5000 candles/interval retention cap). Treat "
            "this backtest as a ~%.0fd window, not %dd — the evidence base is "
            "thinner than the requested days imply.",
            days, interval, cov, cov, days,
        )
    return cov


def build_frames(
    candles_by_coin: dict[str, list[dict[str, Any]]],
    *,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
    spot_candles_by_coin: dict[str, list[dict[str, Any]]] | None = None,
    vwap_window: int = 60,
    warmup: int = 60,
    bar_hours: float = 1.0,
    new_listing_gap_bars: int = 12,
) -> list[Frame]:
    """Assemble aligned per-timestamp frames from per-coin candle series.

    Each output frame carries, per coin: mid (= close), day volume proxy
    (rolling sum), rolling VWAP/sigma (as ``candles_1h``), and the funding rate
    accrued over the bar. Timestamps are the union across coins; coins missing a
    bar are simply absent from that frame.

    HL funding rates are hourly; the engine treats ``Frame.funding`` as the
    *per-bar* rate, so we scale by ``bar_hours`` (= bar interval / 1h). 1h bars
    are unchanged; 5m bars get 1/12 of the hourly rate per bar; 4h bars get 4×.
    Without this, carry PnL is over/understated on any non-1h interval.

    **New-listing signal.** A coin whose first candle is ``>= new_listing_gap_bars``
    bars later than the EARLIEST coin in the dataset (the retention-cliff anchor —
    HL serves ≤~5000 candles/interval, so all established coins start at the same
    cliff) is treated as newly listed. For such coins each frame carries
    ``new_listings[coin] = {age_bars, ref_px, vol_usd, recent_closes}`` computed
    INDEPENDENTLY of the vwap warmup (a day-1 coin has < warmup bars, so its
    candles_1h/closes are absent — without this it would carry no signal at all).
    Requires an established anchor coin in the universe to fix the cliff.
    """
    funding_by_coin = funding_by_coin or {}
    spot_candles_by_coin = spot_candles_by_coin or {}
    # spot close indexed by open-time, per coin (aligned to perp bars by ts).
    spot_close_by_ts: dict[str, dict[int, float]] = {}
    for coin, candles in spot_candles_by_coin.items():
        idx: dict[int, float] = {}
        for k in candles:
            try:
                t = int(k.get("t", 0))
                c = float(k.get("c", 0))
            except (TypeError, ValueError):
                continue
            if c > 0:
                idx[t] = c
        if idx:
            spot_close_by_ts[coin] = idx
    # index each coin's candles by open time
    by_ts: dict[str, dict[int, dict[str, Any]]] = {}
    all_ts: set[int] = set()
    series: dict[str, tuple[list[float], list[float], list[int]]] = {}
    for coin, candles in candles_by_coin.items():
        ordered = sorted(candles, key=lambda k: int(k.get("t", 0)))
        by_ts[coin] = {int(k.get("t", 0)): k for k in ordered}
        series[coin] = _closes_vols(ordered)
        all_ts.update(by_ts[coin].keys())

    # New-listing detection: a coin whose first (valid) candle is materially later
    # than the earliest coin's (the retention cliff, shared by all established
    # coins) was listed DURING the window. Needs an anchor coin at the cliff.
    first_ts_by_coin = {c: tss[0] for c, (_, _, tss) in series.items() if tss}
    global_first_ts = min(first_ts_by_coin.values(), default=0)
    gap_ms = new_listing_gap_bars * int(round(bar_hours * 3_600_000))
    new_coins = {c for c, fts in first_ts_by_coin.items()
                 if fts - global_first_ts >= gap_ms}

    frames: list[Frame] = []
    for ts in sorted(all_ts):
        mids: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles_1h: dict[str, dict] = {}
        closes_window: dict[str, list[float]] = {}
        funding: dict[str, float] = {}
        funding_hourly: dict[str, float] = {}
        spot_mids: dict[str, float] = {}
        new_listings: dict[str, dict] = {}
        for coin, idx in by_ts.items():
            k = idx.get(ts)
            if not k:
                continue
            try:
                mid = float(k.get("c", 0))
            except (TypeError, ValueError):
                continue
            if mid <= 0:
                continue
            mids[coin] = mid
            closes, vols, tss = series[coin]
            upto = [i for i, t in enumerate(tss) if t <= ts]
            # Funding does NOT depend on the vwap warmup — populate it for every
            # bar the coin trades, BEFORE the warmup gate, so the backtester
            # accrues carry on positions held during a coin's first < warmup bars.
            # The day-1 new-listing path enters at age 1–24 (< warmup 30); without
            # this those holds pay/receive zero funding and PnL is misstated for
            # high-funding fresh perps.
            raw_funding = funding_rate_at(funding_by_coin.get(coin, []), ts)
            funding[coin] = raw_funding * bar_hours   # per-bar (PnL accrual)
            funding_hourly[coin] = raw_funding        # unscaled 1h rate (signal)
            # New-listing signal — computed BEFORE the warmup gate so a day-1
            # coin (which has < warmup bars and so no candles_1h/closes below)
            # still carries an age / listing-reference / since-listing volume.
            if upto and coin in new_coins and closes and closes[0] > 0:
                cut0 = upto[-1] + 1
                bpd = max(1, round(24.0 / max(bar_hours, 1e-9)))
                new_listings[coin] = {
                    "age_bars": len(upto),
                    "ref_px": closes[0],
                    "vol_usd": sum(vols[max(0, cut0 - bpd):cut0]) * mid,
                    "recent_closes": list(closes[max(0, cut0 - 48):cut0]),
                }
            if len(upto) < warmup:
                continue
            cut = upto[-1] + 1
            vwap, sigma = rolling_vwap_sigma(closes[:cut], vols[:cut], vwap_window)
            if vwap is not None and sigma is not None:
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": min(cut, vwap_window)}
            closes_window[coin] = closes[max(0, cut - vwap_window):cut]
            bars_per_day = max(1, round(24.0 / max(bar_hours, 1e-9)))
            vol[coin] = sum(vols[max(0, cut - bars_per_day):cut]) * mid  # rolling 24h notional
            # Spot leg: align by timestamp. Expose under spot_mids[coin] AND
            # mids["<coin>-SPOT"] so the engine prices the spot leg via frame.mids
            # (it has no funding entry, so it accrues zero funding — no engine
            # change needed). Coins without a resolvable spot pair are absent.
            spot_close = spot_close_by_ts.get(coin, {}).get(ts)
            if spot_close is not None and spot_close > 0:
                spot_mids[coin] = spot_close
                mids[f"{coin}-SPOT"] = spot_close
        if mids:
            frames.append(Frame(
                ts_ms=ts, mids=mids, funding=funding,
                day_ntl_vlm=vol, candles_1h=candles_1h, closes=closes_window,
                spot_mids=spot_mids, funding_hourly=funding_hourly,
                new_listings=new_listings,
            ))
    return frames


def load_frames(
    coins: list[str],
    *,
    interval: str = "1h",
    days: int = 30,
    with_funding: bool = True,
    base_url: str = "https://api.hyperliquid.xyz",
    vwap_window: int = 60,
) -> list[Frame]:
    """Fetch real history and build frames. Requires network access."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    candles_by_coin: dict[str, list[dict[str, Any]]] = {}
    funding_by_coin: dict[str, list[dict[str, Any]]] = {}
    spot_candles_by_coin: dict[str, list[dict[str, Any]]] = {}
    for coin in coins:
        candles_by_coin[coin] = fetch_candles(coin, interval, start_ms, end_ms, base_url=base_url)
        if with_funding:
            # Page forward past the 500-row cap so funding covers the RECENT end
            # of the window (a single request returns only the oldest ~21d).
            funding_by_coin[coin] = fetch_funding_history_window(
                coin, start_ms, end_ms, base_url=base_url)
        # Spot leg for S4: coins with no resolvable spot pair just won't have it.
        spot = fetch_spot_candles(coin, interval, start_ms, end_ms, base_url=base_url)
        if spot:
            spot_candles_by_coin[coin] = spot
    bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
    frames = build_frames(
        candles_by_coin, funding_by_coin=funding_by_coin,
        spot_candles_by_coin=spot_candles_by_coin,
        vwap_window=vwap_window, warmup=min(vwap_window, 30), bar_hours=bar_hours,
    )
    warn_if_short_coverage(frames, interval=interval, days=days)
    return frames


# ---------------------------------------------------------------------------
# Offline cache (so backtests are reproducible and runnable without network)
# ---------------------------------------------------------------------------


def save_frames(path: str | Path, frames: list[Frame]) -> Path:
    """Persist built frames to a gzipped JSON file (created parent dirs).

    Wrapped with a ``version`` marker (CACHE_VERSION) so a schema bump — e.g.
    v2's spot mids — invalidates older caches built without that data.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "frames": [asdict(f) for f in frames]}
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return p


def load_cached_frames(path: str | Path) -> list[Frame]:
    """Load frames previously written by ``save_frames``.

    Raises ``ValueError`` for legacy (unversioned) or stale-version caches so
    callers refetch — those lacked spot mids (S4).
    """
    with gzip.open(Path(path), "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        raise ValueError("stale or unversioned frame cache; refetch")
    return [Frame(**d) for d in payload["frames"]]


def default_cache_path(coins: list[str], interval: str, days: int) -> Path:
    """Stable on-disk location for a (coins, interval, days) backtest dataset."""
    from ..config import DATA_DIR
    key = f"{'-'.join(sorted(coins))}_{interval}_{days}d"
    return DATA_DIR / "backtest_cache" / f"{key}.json.gz"


def cached_or_fetch(
    coins: list[str],
    *,
    interval: str = "1h",
    days: int = 30,
    base_url: str = "https://api.hyperliquid.xyz",
    cache_path: str | Path | None = None,
    refresh: bool = False,
    vwap_window: int = 60,
) -> list[Frame]:
    """Return frames from cache if present, else fetch (network) and cache them."""
    p = Path(cache_path) if cache_path else default_cache_path(coins, interval, days)
    if p.exists() and not refresh:
        try:
            cached = load_cached_frames(p)
            # Cache hits skip load_frames, so re-emit the coverage warning here
            # — every backtest/confirm/sweep run should see the real window.
            warn_if_short_coverage(cached, interval=interval, days=days)
            return cached
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            # Stale/legacy cache (e.g. pre-spot v1) -> refetch and overwrite.
            log.info("refetching %s: %s", p, exc)
    frames = load_frames(coins, interval=interval, days=days,
                         base_url=base_url, vwap_window=vwap_window)
    save_frames(p, frames)
    return frames


# ---------------------------------------------------------------------------
# Forward frame store (P1 linchpin): rebuild frames from accrued per-bar signal
# ---------------------------------------------------------------------------


def load_accrued_frames(
    conn,
    coins: list[str],
    interval: str,
    *,
    since_ms: int | None = None,
    vwap_window: int = 60,
) -> list[Frame]:
    """Rebuild ``Frame`` objects from the forward ``frame_samples`` store.

    These are the candles HL discards (retention ≤~5000 bars): each cycle the
    engine accrued the rolling vwap/sigma/mid/funding/vol it computed live, so
    replaying them reproduces what the agent saw. One frame per bar timestamp;
    per coin it carries mid, per-bar funding (= hourly × bar_hours), the
    funding_hourly signal, day volume, and ``candles_1h`` = {vwap, sigma, n}
    (the backtester aliases that to the agent's ``candles_<interval>``). ``closes``
    is the trailing window of stored mids. Ordered ascending by bar.
    """
    from collections import defaultdict, deque

    bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
    sql = ["SELECT bar_ts_ms, coin, mid, funding_hourly, vwap, sigma, vol, oi_change",
           "FROM frame_samples WHERE interval = ?"]
    args: list[Any] = [interval]
    if coins:
        sql.append(f"AND coin IN ({','.join('?' * len(coins))})")
        args.extend(coins)
    if since_ms is not None:
        sql.append("AND bar_ts_ms >= ?")
        args.append(int(since_ms))
    sql.append("ORDER BY bar_ts_ms ASC")
    rows = conn.execute(" ".join(sql), args).fetchall()

    frames: list[Frame] = []
    trailing: dict[str, Any] = defaultdict(lambda: deque(maxlen=vwap_window))
    cur_ts: int | None = None
    bucket: list = []

    def _flush(ts: int, bucket: list) -> None:
        mids: dict[str, float] = {}
        funding: dict[str, float] = {}
        funding_hourly: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles: dict[str, dict] = {}
        closes: dict[str, list[float]] = {}
        oi_change: dict[str, float] = {}
        for r in bucket:
            coin = r["coin"]
            mid = r["mid"]
            if mid is None or mid <= 0:
                continue
            mids[coin] = mid
            fh = r["funding_hourly"]
            if fh is not None:
                funding_hourly[coin] = fh
                funding[coin] = fh * bar_hours
            if r["vol"] is not None:
                vol[coin] = r["vol"]
            if r["vwap"] is not None and r["sigma"] is not None:
                candles[coin] = {"vwap": r["vwap"], "sigma": r["sigma"], "n": vwap_window}
            if r["oi_change"] is not None:
                oi_change[coin] = r["oi_change"]
            trailing[coin].append(mid)
            closes[coin] = list(trailing[coin])
        if mids:
            frames.append(Frame(
                ts_ms=ts, mids=mids, funding=funding, day_ntl_vlm=vol,
                candles_1h=candles, closes=closes, funding_hourly=funding_hourly,
                oi_change=oi_change))

    for r in rows:
        if cur_ts is not None and r["bar_ts_ms"] != cur_ts:
            _flush(cur_ts, bucket)
            bucket = []
        cur_ts = r["bar_ts_ms"]
        bucket.append(r)
    if bucket and cur_ts is not None:
        _flush(cur_ts, bucket)
    return frames


def merge_frames(*frame_lists: list[Frame]) -> list[Frame]:
    """Union frames by timestamp, EARLIER lists winning on a tie, sorted
    ascending. Call ``merge_frames(back_fetched, accrued)`` so HL's official
    candles win inside its retention window and accrued frames extend the window
    backward past it — the union grows forward over calendar time."""
    by_ts: dict[int, Frame] = {}
    for fl in frame_lists:
        for f in fl:
            by_ts.setdefault(f.ts_ms, f)
    return [by_ts[t] for t in sorted(by_ts)]


def overlay_oi_change(
    frames: list[Frame],
    oi_by_coin: dict[str, list[tuple[int, float]]],
    *,
    lookback_ms: int = 1_800_000,
) -> int:
    """Overlay cross-venue (Binance) OI-change onto candle frames for the S8
    offline backtest — the host-side way to DETERMINE the OI-crowding edge.

    For each frame and coin with an OI series, sets ``frame.oi_change[coin] =
    (oi@ts - oi@(ts-lookback)) / oi@(ts-lookback)`` using as-of (≤) lookups, the
    same fractional-growth signal ``build_oi_change_view`` computes live. Bars
    without a full lookback of OI history are left without a signal (warmup), so
    S8 simply finds no crowding there. Mutates ``frames`` in place; returns the
    number of (frame, coin) signals written."""
    import bisect

    written = 0
    for coin, series in (oi_by_coin or {}).items():
        if not series:
            continue
        ts_list = [t for t, _ in series]
        oi_list = [o for _, o in series]
        for f in frames:
            i = bisect.bisect_right(ts_list, f.ts_ms) - 1
            j = bisect.bisect_right(ts_list, f.ts_ms - lookback_ms) - 1
            if i < 0 or j < 0 or i == j:
                continue
            oi_now, oi_ref = oi_list[i], oi_list[j]
            if oi_now > 0 and oi_ref > 0:
                f.oi_change[coin] = (oi_now - oi_ref) / oi_ref
                written += 1
    return written
