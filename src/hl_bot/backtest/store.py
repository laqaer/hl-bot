"""Rolling local store of fine-interval Hyperliquid candles (B-HIST).

HL retains only ~``CANDLE_PAGE_LIMIT`` (≈5000) candles per interval — measured
≈3.5d @1m, ≈17d @5m, ≈52d @15m — so live-cadence history *cannot* be fetched
after the fact: it must be appended continuously before it expires. A periodic
job (``hlbot harvest-candles``, run hourly by ``deploy/systemd/
hlbot-harvest.timer``) calls :func:`harvest` to top up one gzipped-JSON file
per (coin, interval) under ``data/candle_store/`` (gitignored). Live-cadence
backtests (B-CAD) read these accumulated series back once they outgrow what
the API can still return.

The merge core is pure (no network/clock) so it is unit-testable offline.
"""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data import (
    CANDLE_PAGE_LIMIT,
    INTERVAL_MS,
    build_frames,
    fetch_candles,
    fetch_funding_history,
)
from .engine import Frame

# 15m is included even though its ~52d retention isn't urgent yet: one extra
# paginated call per coin per run buys the >52d 15m history B-CAD's longer
# walk-forwards will eventually need.
DEFAULT_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m")

# Breadth-validation universe (B-EDGE2d): ten liquid coins OUTSIDE the main
# harvest universe, on which breakout_v1's original-universe G0 PASS failed to
# generalize (fresh-universe OOS −31.5bps taker on the very window the
# original universe earned +70.4). Momentum-family breadth re-tests need this
# history as samples lengthen, and the API's rolling ~52d 15m retention
# destroys it otherwise. 15m ONLY — tripling these coins across 1m/5m would
# double the per-run API load the B-G014 1m sample depends on.
BREADTH_COINS: tuple[str, ...] = (
    "CRV", "ENA", "LIT", "NEAR", "SUI", "TON", "WLD", "XMR", "XPL", "XRP",
)
BREADTH_INTERVALS: tuple[str, ...] = ("15m",)


def store_dir(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    from ..config import DATA_DIR

    return DATA_DIR / "candle_store"


def store_path(coin: str, interval: str, root: str | Path | None = None) -> Path:
    return store_dir(root) / f"{coin}_{interval}.json.gz"


def load_store(path: str | Path) -> list[dict[str, Any]]:
    """Candles previously written by ``save_store`` (missing file → ``[]``)."""
    p = Path(path)
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        out = json.load(fh)
    return out if isinstance(out, list) else []


def save_store(path: str | Path, candles: list[dict[str, Any]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: this file accumulates history that can no longer be
    # refetched, so a crash mid-write must not truncate it.
    tmp = Path(str(p) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(candles, fh)
    tmp.replace(p)
    return p


def _t(row: Any) -> int | None:
    try:
        return int(row.get("t"))
    except (AttributeError, TypeError, ValueError):
        return None


def merge_candles(
    existing: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union by open time ``t``, ascending; ``fresh`` wins on conflict.

    Fresh-wins matters: the newest stored bar may have been captured while
    still forming, and the next harvest refetches it final. Rows without a
    valid integer ``t`` are dropped.
    """
    by_t: dict[int, dict[str, Any]] = {}
    for rows in (existing, fresh):
        for row in rows:
            t = _t(row)
            if t is not None:
                by_t[t] = row
    return [by_t[t] for t in sorted(by_t)]


@dataclass
class HarvestResult:
    coin: str
    interval: str
    added: int = 0
    total: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    error: str | None = None

    @property
    def span_days(self) -> float | None:
        if self.first_ms is None or self.last_ms is None:
            return None
        return (self.last_ms - self.first_ms) / 86_400_000


def harvest_one(
    coin: str,
    interval: str,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
    root: str | Path | None = None,
    now_ms: int | None = None,
    fetch: Callable[..., list[dict[str, Any]]] = fetch_candles,
) -> HarvestResult:
    """Top up one (coin, interval) store file; a fetch failure is recorded, not raised.

    Refetches from the last stored open time *inclusive* so a bar captured
    while still forming is replaced by its final version. An empty store
    starts one full retention window (``CANDLE_PAGE_LIMIT`` bars) back —
    everything HL still has.
    """
    now = int(time.time() * 1000) if now_ms is None else now_ms
    path = store_path(coin, interval, root)
    existing = load_store(path)
    seen = {t for row in existing if (t := _t(row)) is not None}
    step = INTERVAL_MS.get(interval, 60_000)
    start = max(seen) if seen else now - CANDLE_PAGE_LIMIT * step
    res = HarvestResult(coin=coin, interval=interval)
    try:
        fresh = fetch(coin, interval, start, now, base_url=base_url)
    except Exception as e:  # noqa: BLE001 — cron sweep: one bad pair must not kill the rest
        res.error = str(e)
        fresh = []
    merged = merge_candles(existing, fresh)
    if fresh and merged != existing:
        save_store(path, merged)
    res.added = len({t for row in fresh if (t := _t(row)) is not None} - seen)
    res.total = len(merged)
    if merged:
        res.first_ms = _t(merged[0])
        res.last_ms = _t(merged[-1])
    return res


@dataclass
class StoreCoverage:
    """How complete one (coin, interval) store series is over its own span.

    ``missing`` counts interval-aligned gaps between ``first_ms`` and
    ``last_ms`` — a harvester outage longer than HL's retention window loses
    bars forever, and a backtest silently spanning that hole would overstate
    its sample. Surface it instead.
    """

    coin: str
    interval: str
    bars: int
    first_ms: int | None = None
    last_ms: int | None = None
    missing: int = 0

    @property
    def span_days(self) -> float | None:
        if self.first_ms is None or self.last_ms is None:
            return None
        return (self.last_ms - self.first_ms) / 86_400_000

    @property
    def missing_pct(self) -> float:
        expected = self.bars + self.missing
        return self.missing / expected * 100 if expected else 0.0


def coverage_of(coin: str, interval: str, candles: list[dict[str, Any]]) -> StoreCoverage:
    ts = sorted({t for row in candles if (t := _t(row)) is not None})
    cov = StoreCoverage(coin=coin, interval=interval, bars=len(ts))
    if ts:
        cov.first_ms, cov.last_ms = ts[0], ts[-1]
        step = INTERVAL_MS.get(interval, 60_000)
        cov.missing = max(0, (ts[-1] - ts[0]) // step + 1 - len(ts))
    return cov


def frames_from_store(
    coins: list[str],
    *,
    interval: str = "1m",
    days: float = 0.0,
    with_funding: bool = True,
    base_url: str = "https://api.hyperliquid.xyz",
    root: str | Path | None = None,
    vwap_window: int = 60,
    fetch_funding: Callable[..., list[dict[str, Any]]] = fetch_funding_history,
) -> tuple[list[Frame], list[StoreCoverage]]:
    """Build backtest frames from the harvested store instead of the API (B-HIST2).

    The API retains only ~5000 bars per interval; the store accumulates beyond
    that, so this is how live-cadence (1m) backtests outgrow the ~3.5d API
    window. Candles come from ``data/candle_store/``; funding (not
    retention-limited the same way) is still fetched over the candle span,
    seeded 2h early so the carry-forward rate is in effect from the first bar.
    ``days > 0`` trims to the most recent ``days`` before the last stored bar;
    ``days = 0`` uses everything stored. A coin trimmed to nothing stays in the
    returned coverage (bars=0) rather than vanishing silently; a coin with no
    store file at all raises — run ``hlbot harvest-candles`` first.
    """
    candles_by_coin: dict[str, list[dict[str, Any]]] = {}
    missing_pairs: list[str] = []
    for coin in coins:
        rows = load_store(store_path(coin, interval, root))
        if rows:
            candles_by_coin[coin] = rows
        else:
            missing_pairs.append(f"{coin}_{interval}")
    if missing_pairs:
        raise FileNotFoundError(
            f"no stored candles for {', '.join(missing_pairs)} under "
            f"{store_dir(root)}; run `hlbot harvest-candles` and let "
            "hlbot-harvest.timer accumulate history"
        )
    end_ms = max(
        t for rows in candles_by_coin.values() for row in rows
        if (t := _t(row)) is not None
    )
    if days > 0:
        start_ms = end_ms - int(days * 86_400_000)
        candles_by_coin = {
            coin: [row for row in rows if (t := _t(row)) is not None and t >= start_ms]
            for coin, rows in candles_by_coin.items()
        }
    coverage = [coverage_of(c, interval, rows) for c, rows in candles_by_coin.items()]
    funding_by_coin: dict[str, list[dict[str, Any]]] = {}
    if with_funding:
        span_start = min(
            (c.first_ms for c in coverage if c.first_ms is not None), default=end_ms
        )
        for coin, rows in candles_by_coin.items():
            if rows:
                funding_by_coin[coin] = fetch_funding(
                    coin, span_start - 2 * 3_600_000, end_ms, base_url=base_url
                )
    bar_hours = INTERVAL_MS.get(interval, 3_600_000) / 3_600_000
    frames = build_frames(
        candles_by_coin, funding_by_coin=funding_by_coin,
        vwap_window=vwap_window, warmup=min(vwap_window, 30), bar_hours=bar_hours,
    )
    return frames, coverage


def harvest(
    coins: list[str],
    intervals: tuple[str, ...] | list[str] = DEFAULT_INTERVALS,
    *,
    extra_pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    base_url: str = "https://api.hyperliquid.xyz",
    root: str | Path | None = None,
    now_ms: int | None = None,
    fetch: Callable[..., list[dict[str, Any]]] = fetch_candles,
) -> list[HarvestResult]:
    """Sweep the coin × interval grid; per-pair failures land in ``.error``.

    ``extra_pairs`` appends individual (coin, interval) pairs outside the
    cross-product (e.g. the breadth universe at 15m only), deduped against it.
    """
    pairs = [(coin, interval) for coin in coins for interval in intervals]
    for pair in extra_pairs:
        if pair not in pairs:
            pairs.append(pair)
    return [
        harvest_one(coin, interval, base_url=base_url, root=root, now_ms=now_ms, fetch=fetch)
        for coin, interval in pairs
    ]
