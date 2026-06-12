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

from .data import CANDLE_PAGE_LIMIT, INTERVAL_MS, fetch_candles

# 15m is included even though its ~52d retention isn't urgent yet: one extra
# paginated call per coin per run buys the >52d 15m history B-CAD's longer
# walk-forwards will eventually need.
DEFAULT_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m")


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


def harvest(
    coins: list[str],
    intervals: tuple[str, ...] | list[str] = DEFAULT_INTERVALS,
    *,
    base_url: str = "https://api.hyperliquid.xyz",
    root: str | Path | None = None,
    now_ms: int | None = None,
    fetch: Callable[..., list[dict[str, Any]]] = fetch_candles,
) -> list[HarvestResult]:
    """Sweep the coin × interval grid; per-pair failures land in ``.error``."""
    return [
        harvest_one(coin, interval, base_url=base_url, root=root, now_ms=now_ms, fetch=fetch)
        for coin in coins
        for interval in intervals
    ]
