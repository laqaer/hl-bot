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
CACHE_VERSION = 2

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


def fetch_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
) -> list[dict[str, Any]]:
    """Raw candle dicts: {t, T, o, h, l, c, v, n}. Newest-last."""
    with httpx.Client(timeout=20) as client:
        r = client.post(base_url + "/info", json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_ms, "endTime": end_ms},
        })
        r.raise_for_status()
        out = r.json()
    return out if isinstance(out, list) else []


_SPOT_META_CACHE: dict[str, Any] | None = None


def fetch_spot_meta(*, base_url: str = "https://api.hyperliquid.xyz") -> dict[str, Any]:
    """Fetch (and process-cache) the HL spot meta: {"universe": [...], "tokens": [...]}."""
    global _SPOT_META_CACHE
    if _SPOT_META_CACHE is not None:
        return _SPOT_META_CACHE
    with httpx.Client(timeout=20) as client:
        r = client.post(base_url + "/info", json={"type": "spotMeta"})
        r.raise_for_status()
        out = r.json()
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
    """Raw funding rows: {coin, fundingRate, premium, time}."""
    with httpx.Client(timeout=20) as client:
        r = client.post(base_url + "/info", json={
            "type": "fundingHistory", "coin": coin,
            "startTime": start_ms, "endTime": end_ms,
        })
        r.raise_for_status()
        out = r.json()
    return out if isinstance(out, list) else []


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
    spot_candles_by_coin: dict[str, list[dict[str, Any]]] | None = None,
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

    frames: list[Frame] = []
    for ts in sorted(all_ts):
        mids: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles_1h: dict[str, dict] = {}
        closes_window: dict[str, list[float]] = {}
        funding: dict[str, float] = {}
        spot_mids: dict[str, float] = {}
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
            bars_per_day = max(1, round(24.0 / max(bar_hours, 1e-9)))
            vol[coin] = sum(vols[max(0, cut - bars_per_day):cut]) * mid  # rolling 24h notional
            funding[coin] = funding_rate_at(funding_by_coin.get(coin, []), ts) * bar_hours
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
                spot_mids=spot_mids,
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
            funding_by_coin[coin] = fetch_funding_history(coin, start_ms, end_ms, base_url=base_url)
        # Spot leg for S4: coins with no resolvable spot pair just won't have it.
        spot = fetch_spot_candles(coin, interval, start_ms, end_ms, base_url=base_url)
        if spot:
            spot_candles_by_coin[coin] = spot
    bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
    return build_frames(
        candles_by_coin, funding_by_coin=funding_by_coin,
        spot_candles_by_coin=spot_candles_by_coin,
        vwap_window=vwap_window, warmup=min(vwap_window, 30), bar_hours=bar_hours,
    )


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
            return load_cached_frames(p)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            # Stale/legacy cache (e.g. pre-spot v1) -> refetch and overwrite.
            log.info("refetching %s: %s", p, exc)
    frames = load_frames(coins, interval=interval, days=days,
                         base_url=base_url, vwap_window=vwap_window)
    save_frames(p, frames)
    return frames
