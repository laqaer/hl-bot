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


CANDLE_PAGE_LIMIT = 5000  # HL caps candleSnapshot at ~5000 rows/call


def fetch_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw candle dicts: {t, T, o, h, l, c, v, n}, oldest-first.

    Paginated: HL caps ``candleSnapshot`` at ~5000 rows/call, so a long window at
    a fine interval (e.g. 1m/5m over many days) must be walked forward (see
    :func:`paginate_by_time`, keyed on the open time ``t``) or the recent candles
    are silently dropped — the same per-call cap that truncated ``fundingHistory``.
    """
    def _page(s: int, e: int) -> list[dict[str, Any]]:
        with httpx.Client(timeout=20) as client:
            r = client.post(base_url + "/info", json={
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval,
                        "startTime": s, "endTime": e},
            })
            r.raise_for_status()
            out = r.json()
        return out if isinstance(out, list) else []

    return paginate_by_time(
        _page, start_ms, end_ms, page_limit=CANDLE_PAGE_LIMIT, time_key="t",
    )


def paginate_by_time(
    page_fn: Any,
    start_ms: int,
    end_ms: int,
    *,
    page_limit: int = 500,
    time_key: str = "time",
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Walk a time-ordered, page-capped HL endpoint forward to completion.

    HL info endpoints (``fundingHistory``, candle snapshots) return at most
    ``page_limit`` rows per call, oldest-first, starting at ``startTime``. A naive
    single call over a 90d window therefore yields only the *oldest* ~20 days of
    hourly funding and silently drops the rest — which made every carry/FEMR
    backtest over >20d run on stale, carried-forward funding.

    ``page_fn(start, end) -> list[dict]`` fetches one page. We re-request from the
    last row's timestamp + 1ms until a short (``< page_limit``) page arrives, the
    window is exhausted, or no forward progress is made (guard against an endpoint
    that ignores ``startTime``). Rows are de-duplicated by ``time_key`` and
    returned sorted ascending. Pure of network so it is unit-testable with a fake
    ``page_fn`` that simulates the cap.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cur = start_ms
    for _ in range(max_pages):
        page = page_fn(cur, end_ms)
        if not page:
            break
        last_t = cur
        for row in page:
            try:
                t = int(row.get(time_key, 0))
            except (TypeError, ValueError):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(row)
            if t > last_t:
                last_t = t
        if len(page) < page_limit or last_t <= cur or last_t >= end_ms:
            break
        cur = last_t + 1
    out.sort(key=lambda r: int(r.get(time_key, 0) or 0))
    return out


def fetch_funding_history(
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw funding rows: {coin, fundingRate, premium, time}, oldest-first.

    Paginated: HL caps ``fundingHistory`` at 500 rows/call, so a multi-week
    window must be walked forward (see :func:`paginate_by_time`) or the recent
    funding is lost.
    """
    def _page(s: int, e: int) -> list[dict[str, Any]]:
        with httpx.Client(timeout=20) as client:
            r = client.post(base_url + "/info", json={
                "type": "fundingHistory", "coin": coin,
                "startTime": s, "endTime": e,
            })
            r.raise_for_status()
            out = r.json()
        return out if isinstance(out, list) else []

    return paginate_by_time(_page, start_ms, end_ms)


# ---------------------------------------------------------------------------
# Pure transforms (unit-tested)
# ---------------------------------------------------------------------------


def closes_vols(candles: list[dict[str, Any]]) -> tuple[list[float], list[float], list[int]]:
    """Parse candle dicts into (closes, vols, ts) lists, skipping bad rows."""
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

    Each output frame carries, per coin: mid (= close), the bar's intrabar
    high/low (B-FILL2 — fill detection for resting maker quotes; rows with
    missing or inconsistent ``h``/``l`` simply omit the coin and the engine
    degrades to close-only detection), day volume proxy (rolling sum), rolling
    VWAP/sigma (as ``candles_1h``), and the funding rate accrued over the bar.
    Timestamps are the union across coins; coins missing a bar are simply
    absent from that frame.

    HL funding rates are hourly; the engine treats ``Frame.funding`` as the
    *per-bar* rate. Fine bars (≤1h) pro-rate the rate in effect by ``bar_hours``
    (= bar interval / 1h): 1h bars are unchanged, 5m bars get 1/12 per bar.
    Coarse bars (>1h) instead SUM the actual hourly settlements inside the bar —
    extrapolating the last sampled rate ×4/×24 credits an extreme print for a
    full bar when real funding mean-reverts within hours, which systematically
    flatters exactly the carry strategies coarse backtests are used to test.

    Runs in O(total_bars × vwap_window): timestamps are visited in order, so
    per-coin cursors replace the per-frame prefix scans (bars seen so far,
    funding rate in effect) and a volume prefix-sum replaces the per-frame
    1440-bar sum. Fine intervals (5m/1m) over months stay tractable — the
    previous per-frame scans made those quadratic. ``vwap_window`` must be ≥ 2.
    """
    funding_by_coin = funding_by_coin or {}
    # index each coin's candles by open time
    by_ts: dict[str, dict[int, dict[str, Any]]] = {}
    all_ts: set[int] = set()
    series: dict[str, tuple[list[float], list[float], list[int]]] = {}
    vol_prefix: dict[str, list[float]] = {}    # coin -> prefix sums of vols
    bar_cursor: dict[str, int] = {}            # coin -> bars with t <= current ts
    fund_rows: dict[str, list[tuple[int, float]]] = {}  # coin -> (t, rate) by t
    fund_cursor: dict[str, int] = {}
    fund_last: dict[str, float] = {}           # coin -> rate in effect at cursor
    for coin, candles in candles_by_coin.items():
        ordered = sorted(candles, key=lambda k: int(k.get("t", 0)))
        by_ts[coin] = {int(k.get("t", 0)): k for k in ordered}
        series[coin] = closes_vols(ordered)
        all_ts.update(by_ts[coin].keys())
        vols = series[coin][1]
        prefix = [0.0] * (len(vols) + 1)
        for i, v in enumerate(vols):
            prefix[i + 1] = prefix[i] + v
        vol_prefix[coin] = prefix
        bar_cursor[coin] = 0
        rows: list[tuple[int, float]] = []
        for row in funding_by_coin.get(coin, []):
            try:
                rows.append((int(row.get("time", 0)), float(row.get("fundingRate", 0) or 0)))
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda r: r[0])
        fund_rows[coin] = rows
        fund_cursor[coin] = 0
        fund_last[coin] = 0.0

    frames: list[Frame] = []
    for ts in sorted(all_ts):
        mids: dict[str, float] = {}
        highs: dict[str, float] = {}
        lows: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles_1h: dict[str, dict] = {}
        closes_window: dict[str, list[float]] = {}
        funding: dict[str, float] = {}
        funding_hourly: dict[str, float] = {}
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
            try:
                hi = float(k.get("h", 0) or 0)
                lo_px = float(k.get("l", 0) or 0)
            except (TypeError, ValueError):
                hi = lo_px = 0.0
            if 0 < lo_px <= hi:
                highs[coin] = hi
                lows[coin] = lo_px
            closes, vols, tss = series[coin]
            cur = bar_cursor[coin]
            while cur < len(tss) and tss[cur] <= ts:
                cur += 1
            bar_cursor[coin] = cut = cur
            if cut < warmup:
                continue
            lo = max(0, cut - vwap_window)
            vwap, sigma = rolling_vwap_sigma(closes[lo:cut], vols[lo:cut], vwap_window)
            if vwap is not None and sigma is not None:
                candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": min(cut, vwap_window)}
            closes_window[coin] = closes[lo:cut]
            prefix = vol_prefix[coin]
            # ~rolling notional proxy over the trailing 1440 bars
            vol[coin] = (prefix[cut] - prefix[max(0, cut - 1440)]) * mid
            rates = fund_rows[coin]
            fi = fund_cursor[coin]
            fund_sum = 0.0
            bar_start = ts - int(bar_hours * 3_600_000)
            while fi < len(rates) and rates[fi][0] <= ts:
                fund_last[coin] = rates[fi][1]
                if rates[fi][0] > bar_start:
                    fund_sum += rates[fi][1]
                fi += 1
            fund_cursor[coin] = fi
            funding[coin] = fund_sum if bar_hours > 1.0 else fund_last[coin] * bar_hours
            funding_hourly[coin] = fund_last[coin]
        if mids:
            frames.append(Frame(
                ts_ms=ts, mids=mids, funding=funding, funding_hourly=funding_hourly,
                day_ntl_vlm=vol, candles_1h=candles_1h, closes=closes_window,
                highs=highs, lows=lows,
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


def ensure_funding_hourly(frames: list[Frame], bar_hours: float) -> list[Frame]:
    """Backfill ``funding_hourly`` on frames cached before the field existed.

    ``Frame.funding`` is the per-bar rate (hourly × ``bar_hours``), so dividing
    recovers the hourly series exactly. Without this, a legacy fine-interval
    cache would feed agents rates 60× (1m) too small via the engine fallback,
    and any funding-threshold lever would A/B against the wrong units.
    """
    if bar_hours <= 0:
        return frames
    for f in frames:
        if f.funding and not f.funding_hourly:
            f.funding_hourly = {c: r / bar_hours for c, r in f.funding.items()}
    return frames


def default_cache_path(
    coins: list[str], interval: str, days: int, vwap_window: int = 60
) -> Path:
    """Stable on-disk location for a (coins, interval, days, window) dataset.

    Cached frames bake the VWAP window into ``candles_1h``/``closes``, so a
    non-default ``vwap_window`` MUST enter the key — otherwise a window-60
    dataset would silently serve a window-4 run (B-CAD). The default window
    keeps the historical key so existing caches stay valid.
    """
    from ..config import DATA_DIR
    key = f"{'-'.join(sorted(coins))}_{interval}_{days}d"
    if vwap_window != 60:
        key += f"_w{vwap_window}"
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
    p = Path(cache_path) if cache_path else default_cache_path(coins, interval, days, vwap_window)
    if p.exists() and not refresh:
        bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
        return ensure_funding_hourly(load_cached_frames(p), bar_hours)
    frames = load_frames(coins, interval=interval, days=days,
                         base_url=base_url, vwap_window=vwap_window)
    save_frames(p, frames)
    return frames
