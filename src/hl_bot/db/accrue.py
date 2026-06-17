"""Append-only accrual of market state to SQLite for forward confirmation.

These functions are intentionally thin: they take the same `MarketView` that
agents already consume and write it to the DB. The forward-confirmation loop
later reconstructs `Frame`s from these rows.
"""

from __future__ import annotations

import json
import sqlite3

from ..agents.base import MarketView
from ..backtest.engine import Frame


def accrue_market_snapshot(conn: sqlite3.Connection, view: MarketView) -> int:
    """Write one row per coin from *view* to `market_snapshots`.

    Returns the number of rows written. Uses upsert on (ts_ms, coin).
    """
    ts = view.ts_ms
    rows = 0
    for coin, mid in view.mids.items():
        funding = view.funding.get(coin)
        oi = view.open_interest.get(coin)
        vol = view.extra.get("day_ntl_vlm", {}).get(coin)
        book = view.book_top.get(coin)
        bid, ask = (book[0], book[1]) if book else (None, None)
        # No size in MarketView book_top yet; leave sz columns NULL.
        raw = {
            "mid": mid,
            "funding": funding,
            "open_interest": oi,
            "day_ntl_vlm": vol,
            "book_top": book,
            "candles_1h": view.extra.get("candles_1h", {}).get(coin),
        }
        conn.execute(
            """
            INSERT INTO market_snapshots(
                ts_ms, coin, mid, funding_1h, open_interest, day_ntl_vlm,
                book_bid, book_ask, book_bid_sz, book_ask_sz, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ts_ms, coin) DO UPDATE SET
                mid=excluded.mid,
                funding_1h=excluded.funding_1h,
                open_interest=excluded.open_interest,
                day_ntl_vlm=excluded.day_ntl_vlm,
                book_bid=excluded.book_bid,
                book_ask=excluded.book_ask,
                book_bid_sz=excluded.book_bid_sz,
                book_ask_sz=excluded.book_ask_sz,
                raw_json=excluded.raw_json
            """,
            (
                ts, coin, mid, funding, oi, vol,
                bid, ask, None, None,
                json.dumps(raw, separators=(",", ":")),
            ),
        )
        rows += 1
    return rows


def load_forward_frames(
    conn: sqlite3.Connection,
    coins: list[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Frame]:
    """Reconstruct `Frame`s from `market_snapshots` for forward confirmation.

    If *coins* is None, all coins with snapshots are included.  The returned
    frames are sorted by timestamp and include the last `closes` per coin so
    rolling stats can be recomputed by the caller if needed.
    """
    params: list = []
    sql = "SELECT * FROM market_snapshots WHERE 1=1"
    if start_ms is not None:
        sql += " AND ts_ms >= ?"
        params.append(start_ms)
    if end_ms is not None:
        sql += " AND ts_ms <= ?"
        params.append(end_ms)
    if coins:
        placeholders = ",".join("?" for _ in coins)
        sql += f" AND coin IN ({placeholders})"
        params.extend(coins)
    sql += " ORDER BY ts_ms"

    rows = conn.execute(sql, params).fetchall()
    by_ts: dict[int, dict[str, dict]] = {}
    for r in rows:
        ts = r["ts_ms"]
        by_ts.setdefault(ts, {})[r["coin"]] = {
            "mid": r["mid"],
            "funding": r["funding_1h"],
            "oi": r["open_interest"],
            "vol": r["day_ntl_vlm"],
        }

    frames: list[Frame] = []
    closes_by_coin: dict[str, list[float]] = {}
    for ts in sorted(by_ts):
        mids: dict[str, float] = {}
        funding: dict[str, float] = {}
        vol: dict[str, float] = {}
        candles_1h: dict[str, dict] = {}
        closes_window: dict[str, list[float]] = {}
        for coin, data in by_ts[ts].items():
            mid = data["mid"]
            if mid is None:
                continue
            mids[coin] = mid
            closes_by_coin.setdefault(coin, []).append(mid)
            closes_window[coin] = closes_by_coin[coin][-60:]
            if data["funding"] is not None:
                funding[coin] = data["funding"]
            if data["vol"] is not None:
                vol[coin] = data["vol"]
            # Prefer the live-captured vwap/sigma stored in raw_json; fall back
            # to a simple trailing-close estimate if unavailable.
            stored_candle = (r["raw_json"] and __import__("json").loads(r["raw_json"]).get("candles_1h")) or {}
            vwap = stored_candle.get("vwap")
            sigma = stored_candle.get("sigma")
            if vwap is None or sigma is None:
                closes_list = closes_window[coin]
                if closes_list:
                    vwap = sum(closes_list) / len(closes_list)
                    mean = vwap
                    variance = sum((p - mean) ** 2 for p in closes_list) / len(closes_list)
                    sigma = variance ** 0.5 if variance > 0 else 0.0
                else:
                    vwap = mid
                    sigma = 0.0
            candles_1h[coin] = {"vwap": vwap, "sigma": sigma, "n": len(closes_window[coin])}
        if mids:
            frames.append(Frame(
                ts_ms=ts, mids=mids, funding=funding,
                day_ntl_vlm=vol, candles_1h=candles_1h, closes=closes_window,
            ))
    return frames
