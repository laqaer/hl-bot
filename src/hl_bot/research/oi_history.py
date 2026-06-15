"""Binance open-interest history — the offline data S8 needs to be DETERMINED.

HL serves OI only as a live snapshot (``metaAndAssetCtxs``), never in candles, so
``oi_crowding_reversal`` (S8) cannot be back-tested on HL data — it can only be
confirmed forward, which takes calendar time. Binance, by contrast, publishes
~30 days of historical open interest (``futures/data/openInterestHist``, 5m
granularity). Using it as a CROSS-VENUE PROXY for HL crowding lets us measure
whether the S8 edge is real BEFORE committing weeks of forward soak — the same
phase-1-offline-study posture as the xvenue funding work (S5).

Binance is geo-blocked from the CI sandbox, so :func:`fetch_binance_oi_hist` is
host-only (and ``# pragma: no cover``); the parsing/pagination is pulled into a
pure function so it is fully unit-tested. Symbol mapping reuses
``funding_xvenue.hl_to_binance`` (kPEPE -> 1000PEPEUSDT, HL-natives -> None).
"""

from __future__ import annotations

import logging

from .funding_xvenue import hl_to_binance

log = logging.getLogger(__name__)

_BINANCE_OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"
_MAX_LIMIT = 500  # Binance hard cap per call
_PERIOD_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
              "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}


def parse_binance_oi(rows: object) -> list[tuple[int, float]]:
    """Binance ``openInterestHist`` payload -> sorted ``[(ts_ms, oi)]``.

    Each row: ``{"symbol","sumOpenInterest","sumOpenInterestValue","timestamp"}``.
    We use ``sumOpenInterest`` (base-asset units) — the FRACTIONAL change is what
    S8 reads, so units cancel. Bad/zero/invalid rows are dropped; the result is
    de-duplicated on timestamp and sorted ascending."""
    if not isinstance(rows, list):
        return []
    by_ts: dict[int, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ts = int(r["timestamp"])
            oi = float(r["sumOpenInterest"])
        except (KeyError, TypeError, ValueError):
            continue
        if oi > 0:
            by_ts[ts] = oi
    return sorted(by_ts.items())


def fetch_binance_oi_hist(  # pragma: no cover - network (geo-blocked from CI)
    coin: str,
    *,
    period: str = "5m",
    days: int = 30,
    timeout: float = 10.0,
    now_ms: int | None = None,
) -> list[tuple[int, float]]:
    """Host-only: fetch ~``days`` of Binance OI history for an HL ``coin``.

    Paginates backward via ``endTime`` (Binance returns the most recent ``limit``
    points at/before it) until the window is covered or the data runs out.
    Returns sorted ``[(ts_ms, oi)]``, or ``[]`` for an unmapped/HL-native coin or
    any network failure (best-effort, never raises)."""
    import time

    import httpx

    sym = hl_to_binance(coin)
    if sym is None:
        return []
    step_ms = _PERIOD_MS.get(period, 300_000)
    now_ms = now_ms or int(time.time() * 1000)
    start_floor = now_ms - days * 86_400_000
    end_time = now_ms
    merged: dict[int, float] = {}
    try:
        with httpx.Client(timeout=timeout) as cli:
            for _ in range(days * 24 * 60 * 60 * 1000 // (step_ms * _MAX_LIMIT) + 2):
                rows = cli.get(_BINANCE_OI_URL, params={
                    "symbol": sym, "period": period,
                    "limit": _MAX_LIMIT, "endTime": end_time,
                }).json()
                pts = parse_binance_oi(rows)
                if not pts:
                    break
                for ts, oi in pts:
                    merged[ts] = oi
                earliest = pts[0][0]
                if earliest <= start_floor:
                    break
                end_time = earliest - 1
    except Exception as e:  # noqa: BLE001
        log.debug("binance OI fetch failed for %s: %s", coin, e)
    return [(ts, oi) for ts, oi in sorted(merged.items()) if ts >= start_floor]
