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
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from .engine import Frame

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

# Status codes worth retrying: 429 (rate limit) + transient 5xx.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_delay(
    resp: httpx.Response, attempt: int, base_delay: float, max_delay: float
) -> float:
    """Seconds to wait before the next attempt: honor ``Retry-After`` if present,
    else exponential backoff (``base_delay * 2**attempt``), capped at ``max_delay``."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), max_delay)
        except ValueError:
            pass
    return min(base_delay * (2 ** attempt), max_delay)


def _request_with_retry(
    do_request: Callable[[], httpx.Response],
    *,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Call ``do_request()``, retrying on 429/5xx with exponential backoff.

    A longer/larger backtest window means more candle requests *and* more funding
    pages, which reliably trips HL's rate limiter (429) — and a 429 mid-window
    used to lose the whole window. This retries transient failures (honoring a
    ``Retry-After`` header when present) so the fetch completes. On a non-retryable
    status it raises immediately; once retries are exhausted it surfaces the last
    error via ``raise_for_status``. Pure given ``do_request`` + ``sleep``, so it is
    unit-tested without a network or real clock.
    """
    attempt = 0
    while True:
        resp = do_request()
        if resp.status_code not in _RETRY_STATUSES or attempt >= max_retries:
            resp.raise_for_status()
            return resp
        sleep(_retry_delay(resp, attempt, base_delay, max_delay))
        attempt += 1


def fetch_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw candle dicts: {t, T, o, h, l, c, v, n}. Newest-last."""
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }
    with httpx.Client(timeout=20) as client:
        r = _request_with_retry(lambda: client.post(base_url + "/info", json=payload))
        out = r.json()
    return out if isinstance(out, list) else []


def _fetch_funding_page(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """One funding page (HL caps this at 500 rows, oldest-first from start_ms)."""
    payload = {
        "type": "fundingHistory", "coin": coin,
        "startTime": start_ms, "endTime": end_ms,
    }
    with httpx.Client(timeout=20) as client:
        r = _request_with_retry(lambda: client.post(base_url + "/info", json=payload))
        out = r.json()
    return out if isinstance(out, list) else []


def _paginate_funding(
    fetch_page: Any, start_ms: int, end_ms: int, *, max_pages: int = 64
) -> list[dict[str, Any]]:
    """Walk a 500-row-capped page fetcher to cover the whole [start, end] window.

    ``fetch_page(start, end)`` returns one oldest-first page (≤500 rows). We
    advance the cursor past the last row each page and stop on: empty page, a
    short page (<500 → last page), no time progress, or ``max_pages``. Dedupes by
    ``time`` so a boundary row repeated across pages is counted once. Pure given
    ``fetch_page`` — unit-tested without a network.
    """
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    cursor = start_ms
    for _ in range(max_pages):
        page = fetch_page(cursor, end_ms)
        if not page:
            break
        for row in page:
            try:
                t = int(row.get("time", 0))
            except (TypeError, ValueError):
                continue
            if t in seen:
                continue
            seen.add(t)
            rows.append(row)
        last_t = max((int(r.get("time", 0)) for r in page), default=cursor)
        nxt = last_t + 1
        if len(page) < 500 or nxt <= cursor or nxt > end_ms:
            break
        cursor = nxt
    return rows


def fetch_funding_history(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw funding rows {coin, fundingRate, premium, time} over the full window.

    HL's ``fundingHistory`` returns at most 500 rows (~20.8d at the hourly
    cadence), so a single call silently truncates any longer backtest window —
    every frame older than the last 500h would otherwise read funding=0, which is
    fatal for *carry* measurement. Page through to cover the whole window.
    """
    return _paginate_funding(
        lambda s, e: _fetch_funding_page(coin, s, e, base_url=base_url),
        start_ms, end_ms,
    )


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


def build_frames(
    candles_by_coin: dict[str, list[dict[str, Any]]],
    *,
    funding_by_coin: dict[str, list[dict[str, Any]]] | None = None,
    vwap_window: int = 60,
    warmup: int = 60,
    bar_hours: float = 1.0,
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
    """
    funding_by_coin = funding_by_coin or {}
    # index each coin's candles by open time
    by_ts: dict[str, dict[int, dict[str, Any]]] = {}
    all_ts: set[int] = set()
    series: dict[str, tuple[list[float], list[float], list[int]]] = {}
    for coin, candles in candles_by_coin.items():
        ordered = sorted(candles, key=lambda k: int(k.get("t", 0)))
        by_ts[coin] = {int(k.get("t", 0)): k for k in ordered}
        series[coin] = _closes_vols(ordered)
        all_ts.update(by_ts[coin].keys())

    frames: list[Frame] = []
    for ts in sorted(all_ts):
        mids: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles_1h: dict[str, dict] = {}
        closes_window: dict[str, list[float]] = {}
        funding: dict[str, float] = {}
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
            if len(upto) < warmup:
                continue
            cut = upto[-1] + 1
            vwap, sigma = rolling_vwap_sigma(closes[:cut], vols[:cut], vwap_window)
            if vwap is not None and sigma is not None:
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": min(cut, vwap_window)}
            closes_window[coin] = closes[max(0, cut - vwap_window):cut]
            vol[coin] = sum(vols[max(0, cut - 1440):cut]) * mid  # ~rolling notional proxy
            funding[coin] = funding_rate_at(funding_by_coin.get(coin, []), ts) * bar_hours
        if mids:
            frames.append(Frame(
                ts_ms=ts, mids=mids, funding=funding,
                day_ntl_vlm=vol, candles_1h=candles_1h, closes=closes_window,
            ))
    return frames


def window_bounds(days: int, end_ms: int | None = None) -> tuple[int, int]:
    """[start, end] ms for a ``days``-long window ending at ``end_ms`` (default now).

    Pure (modulo the default clock) so out-of-time windows are unit-testable: pass
    an explicit ``end_ms`` in the past to fetch a *disjoint, older* window for
    out-of-sample validation instead of always the trailing ``days``.
    """
    end = int(time.time() * 1000) if end_ms is None else int(end_ms)
    return end - days * 86_400_000, end


def load_frames(
    coins: list[str],
    *,
    interval: str = "1h",
    days: int = 30,
    with_funding: bool = True,
    base_url: str = "https://api.hyperliquid.xyz",
    vwap_window: int = 60,
    end_ms: int | None = None,
) -> list[Frame]:
    """Fetch real history and build frames. Requires network access.

    ``end_ms`` (default now) lets callers pull a *historical* window ending in the
    past — used for out-of-time validation (re-confirm an edge on a fresh, disjoint
    window). The trailing-window behavior is unchanged when ``end_ms`` is None.
    """
    start_ms, end_ms = window_bounds(days, end_ms)
    candles_by_coin: dict[str, list[dict[str, Any]]] = {}
    funding_by_coin: dict[str, list[dict[str, Any]]] = {}
    for coin in coins:
        candles_by_coin[coin] = fetch_candles(coin, interval, start_ms, end_ms, base_url=base_url)
        if with_funding:
            funding_by_coin[coin] = fetch_funding_history(coin, start_ms, end_ms, base_url=base_url)
    bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
    return build_frames(
        candles_by_coin, funding_by_coin=funding_by_coin,
        vwap_window=vwap_window, warmup=min(vwap_window, 30), bar_hours=bar_hours,
    )


# ---------------------------------------------------------------------------
# Offline cache (so backtests are reproducible and runnable without network)
# ---------------------------------------------------------------------------


def save_frames(path: str | Path, frames: list[Frame]) -> Path:
    """Persist built frames to a gzipped JSON file (created parent dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(f) for f in frames]
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return p


def load_cached_frames(path: str | Path) -> list[Frame]:
    """Load frames previously written by ``save_frames``."""
    with gzip.open(Path(path), "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return [Frame(**d) for d in payload]


def default_cache_path(
    coins: list[str], interval: str, days: int, end_ms: int | None = None
) -> Path:
    """Stable on-disk location for a (coins, interval, days[, end]) backtest dataset.

    A trailing (``end_ms is None``) window keeps the original key so existing
    caches still resolve. A historical window is tagged with its end date so an
    out-of-time fetch lands in a *distinct* file and can't collide with the
    trailing cache for the same coins/interval/days.
    """
    from ..config import DATA_DIR
    key = f"{'-'.join(sorted(coins))}_{interval}_{days}d"
    if end_ms is not None:
        day = time.strftime("%Y%m%d", time.gmtime(int(end_ms) / 1000))
        key += f"_end{day}"
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
    end_ms: int | None = None,
) -> list[Frame]:
    """Return frames from cache if present, else fetch (network) and cache them.

    ``end_ms`` selects a historical (out-of-time) window; it is folded into the
    default cache key so trailing and historical windows never share a file.
    """
    p = Path(cache_path) if cache_path else default_cache_path(coins, interval, days, end_ms)
    if p.exists() and not refresh:
        return load_cached_frames(p)
    frames = load_frames(coins, interval=interval, days=days,
                         base_url=base_url, vwap_window=vwap_window, end_ms=end_ms)
    save_frames(p, frames)
    return frames
