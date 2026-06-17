"""New-listing detection and first-seen capture.

Hyperliquid periodically lists new perps. This module detects coins that have
not been seen before and records their first mid plus an initial candle window
so future agents (e.g. new-listing reversion) have data from day one.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from ..agents.base import MarketView

FetchCandlesFn = Callable[[str, str, int, int, str], list[dict[str, Any]]]


def _default_fetch_candles(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    base_url: str,
) -> list[dict[str, Any]]:
    """Default candle fetcher using the backtest data module."""
    from ..backtest.data import fetch_candles

    return fetch_candles(coin, interval, start_ms, end_ms, base_url=base_url)


def detect_new_listings(
    conn: sqlite3.Connection,
    view: MarketView,
    base_url: str,
    *,
    fetch_candles_fn: FetchCandlesFn | None = None,
) -> list[str]:
    """Detect coins in *view* that are not yet in `new_listings`, insert them,
    and fetch their first 24h of 1h candles.

    Returns the list of newly seen coins.
    """
    fetch_candles_fn = fetch_candles_fn or _default_fetch_candles
    existing = {
        r["coin"]
        for r in conn.execute("SELECT coin FROM new_listings").fetchall()
    }
    new: list[str] = []
    for coin, mid in view.mids.items():
        if coin in existing:
            continue
        new.append(coin)
        end_ms = view.ts_ms
        start_ms = end_ms - 24 * 3_600_000
        try:
            candles = fetch_candles_fn(coin, "1h", start_ms, end_ms, base_url)
        except Exception:  # noqa: BLE001
            candles = []
        conn.execute(
            """
            INSERT INTO new_listings(
                coin, first_seen_ms, first_listed_px, initial_candles_json, meta_json
            ) VALUES(?,?,?,?,?)
            """,
            (
                coin,
                view.ts_ms,
                mid,
                json.dumps(candles, separators=(",", ":")) if candles else None,
                json.dumps({"candles_fetched": len(candles)}, separators=(",", ":")),
            ),
        )
    return new


def fetch_initial_candles(
    coin: str,
    base_url: str,
    end_ms: int | None = None,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """Convenience helper: fetch the first *hours* of 1h candles for *coin*."""
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = end_ms - hours * 3_600_000
    from ..backtest.data import fetch_candles

    return fetch_candles(coin, "1h", start_ms, end_ms, base_url=base_url)
