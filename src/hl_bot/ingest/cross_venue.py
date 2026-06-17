"""Cross-venue funding-rate ingest (Binance, Bybit) for crowding signals.

Hyperliquid funding is already captured in `market_snapshots.funding_1h`. This
module accrues comparable 1h funding rates from Binance and Bybit so future
agents can compare funding extremes across venues.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding-history"


def _hl_to_usdt(coin: str) -> str | None:
    """Map HL perp coin symbol to the USDT-linear perpetual symbol on other venues."""
    # Stablecoins and indices unlikely to have perps elsewhere.
    if not coin or coin.endswith("USD") or coin in ("USDC", "USDT"):
        return None
    return f"{coin}USDT"


def fetch_binance_funding(coin: str, limit: int = 1) -> list[dict[str, Any]]:
    """Return recent 1h funding rates for *coin* from Binance.

    Each dict has `ts_ms` and `funding_1h`.
    """
    symbol = _hl_to_usdt(coin)
    if symbol is None:
        return []
    with httpx.Client(timeout=15) as client:
        r = client.get(BINANCE_FUNDING_URL, params={"symbol": symbol, "limit": limit})
        r.raise_for_status()
        rows = r.json()
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            funding_time = int(row.get("fundingTime", 0))
            rate = float(row.get("fundingRate", 0))
        except (TypeError, ValueError):
            continue
        if funding_time > 0:
            out.append({"ts_ms": funding_time, "funding_1h": rate})
    return out


def fetch_bybit_funding(coin: str, limit: int = 1) -> list[dict[str, Any]]:
    """Return recent 1h funding rates for *coin* from Bybit."""
    symbol = _hl_to_usdt(coin)
    if symbol is None:
        return []
    with httpx.Client(timeout=15) as client:
        r = client.get(
            BYBIT_FUNDING_URL,
            params={"category": "linear", "symbol": symbol, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
    rows = data.get("result", {}).get("list", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            funding_time = int(row.get("fundingRateTimestamp", 0))
            rate = float(row.get("fundingRate", 0))
        except (TypeError, ValueError):
            continue
        if funding_time > 0:
            out.append({"ts_ms": funding_time, "funding_1h": rate})
    return out


def accrue_cross_venue_funding(
    conn: sqlite3.Connection,
    coins: list[str],
    *,
    venues: list[str] | None = None,
) -> dict[str, int]:
    """Fetch and upsert cross-venue funding for *coins*.

    Returns a count map `{venue: rows_written}`.
    """
    venues = venues or ["binance", "bybit"]
    fetchers: dict[str, Any] = {
        "binance": fetch_binance_funding,
        "bybit": fetch_bybit_funding,
    }
    counts: dict[str, int] = {v: 0 for v in venues}
    for coin in coins:
        for venue in venues:
            rows = fetchers[venue](coin)
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO funding_cross_venue(ts_ms, coin, venue, funding_1h)
                    VALUES(?,?,?,?)
                    ON CONFLICT(ts_ms, coin, venue) DO UPDATE SET
                        funding_1h=excluded.funding_1h
                    """,
                    (row["ts_ms"], coin, venue, row["funding_1h"]),
                )
                counts[venue] += 1
    return counts
