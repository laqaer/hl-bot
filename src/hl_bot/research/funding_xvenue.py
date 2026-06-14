"""S5 — cross-venue funding signal (phase 1: signal only).

Funding on one venue mean-reverts toward the cross-venue consensus. When HL
funding is far above Binance/Bybit funding for the *same* coin, HL's print is
the outlier (venue-local crowding) — a cleaner, more selective carry entry
signal than HL funding alone, and it's free (public, no-auth endpoints). See
`docs/research/S5_xvenue_funding.md` and `docs/ALPHA_ROADMAP.md` §2.

Design mirrors `ingest/ws.py`: pure parse/normalize/score functions (unit-tested
with synthetic payloads) plus thin network wrappers (`pragma: no cover`). Phase 1
adds **zero execution cost** and is **off by default** — it only changes WHICH
trades the existing carry agents take, and only once an operator opts in
(`require_xvenue_spread_bps` in agent config) after the offline study confirms
selectivity. Nothing here places orders.

Unit conventions
----------------
HL `MarketView.funding[coin]` is a **per-hour** rate. Binance/Bybit publish a
per-**funding-interval** rate (usually 8h). Everything is normalized to per-hour
internally; human-facing thresholds are expressed in **bps per 8h** (the spec's
5/10/20 bps), via `per_hr_to_bps_8h`.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Assumed funding interval for Binance/Bybit when normalizing to per-hour. Most
# USDT-perp listings settle every 8h; a handful settle 4h/1h. Phase-1 treats the
# consensus as a coarse fair-value anchor, so the common case is sufficient; the
# offline study (and any phase-2 arb) must read the per-symbol interval.
_DEFAULT_FUNDING_INTERVAL_HRS = 8.0


# ---------------------------------------------------------------------------
# Symbol mapping — the named hazard (S5 spec "Risk"). kPEPE vs 1000PEPE etc.
# ---------------------------------------------------------------------------

# HL prefixes its 1000x-denominated meme tokens with a lowercase "k"; Binance
# and Bybit prefix the same tokens with "1000". An explicit table documents the
# known cases; `_normalize_base` also applies the general k-> 1000 rule so a new
# HL kXYZ listing maps without a code change. Plain majors (BTC, ETH, SOL, …)
# map straight through. Anything HL-native (HYPE, PURR) has no off-venue symbol
# and simply yields no consensus (fail-open in the filter).
SYMBOL_OVERRIDES: dict[str, str] = {
    "kPEPE": "1000PEPE",
    "kBONK": "1000BONK",
    "kSHIB": "1000SHIB",
    "kFLOKI": "1000FLOKI",
    "kLUNC": "1000LUNC",
    "kDOGS": "1000DOGS",
    "kNEIRO": "1000NEIRO",
    "kCAT": "1000CAT",
}

# Coins known to have no Binance/Bybit USDT-perp listing — skip the lookup so we
# don't emit misleading requests. Not exhaustive; the fetchers also fail-open.
NO_OFFVENUE: frozenset[str] = frozenset({"HYPE", "PURR"})


def _normalize_base(coin: str) -> str:
    """HL coin -> the off-venue base asset (no quote suffix).

    Explicit overrides win; otherwise apply the general lowercase-``k`` -> 1000
    rule (``kWHATEVER`` -> ``1000WHATEVER``); otherwise pass through unchanged.
    """
    if coin in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[coin]
    if len(coin) > 1 and coin[0] == "k" and coin[1:].isupper():
        return "1000" + coin[1:]
    return coin


def hl_to_binance(coin: str) -> str | None:
    """HL coin -> Binance USDT-perp symbol (e.g. ``kPEPE`` -> ``1000PEPEUSDT``)."""
    if coin in NO_OFFVENUE:
        return None
    return _normalize_base(coin) + "USDT"


def hl_to_bybit(coin: str) -> str | None:
    """HL coin -> Bybit linear-perp symbol (same convention as Binance USDT)."""
    if coin in NO_OFFVENUE:
        return None
    return _normalize_base(coin) + "USDT"


# ---------------------------------------------------------------------------
# Normalization + scoring (pure)
# ---------------------------------------------------------------------------


def per_hr_from_interval(rate: float, interval_hrs: float = _DEFAULT_FUNDING_INTERVAL_HRS) -> float:
    """Per-interval funding rate -> per-hour rate."""
    if interval_hrs <= 0:
        return 0.0
    return rate / interval_hrs


def per_hr_to_bps_8h(rate_per_hr: float) -> float:
    """Per-hour rate -> basis points per 8h (the spec's threshold unit)."""
    return rate_per_hr * 8.0 * 1e4


def consensus_per_hr(xv: dict[str, float] | None) -> float | None:
    """Cross-venue consensus (mean of available venue per-hour rates), or None.

    ``xv`` is the per-coin entry from MarketView extra ``funding_xvenue``:
    ``{"binance": r_per_hr, "bybit": r_per_hr}``. Missing/None venues are
    skipped; an empty dict yields None (no consensus -> fail-open downstream).
    """
    if not xv:
        return None
    vals = [float(v) for v in xv.values() if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def xvenue_spread_per_hr(hl_funding_per_hr: float, xv: dict[str, float] | None) -> float | None:
    """HL per-hour funding minus the cross-venue consensus, or None if no
    consensus is available."""
    cons = consensus_per_hr(xv)
    if cons is None:
        return None
    return hl_funding_per_hr - cons


def passes_xvenue_filter(
    hl_funding_per_hr: float,
    xv: dict[str, float] | None,
    *,
    min_spread_bps_8h: float,
) -> bool:
    """Selectivity filter for a carry *entry* on ``coin``.

    True (allow the trade) when HL funding is more extreme than the cross-venue
    consensus **in the carry-collecting direction** by at least
    ``min_spread_bps_8h``. The carry direction is the sign of HL funding (short
    collects positive funding, long collects negative), so we require
    ``sign(hl) * (hl - consensus) >= threshold``.

    **Fail-open:** when no cross-venue consensus is available (HL-native coin,
    network miss, unmapped symbol) this returns True — the signal is purely
    additive and must never silently halt all trading when data is absent.
    """
    spread = xvenue_spread_per_hr(hl_funding_per_hr, xv)
    if spread is None:
        return True  # no data -> don't block
    directional = spread if hl_funding_per_hr >= 0 else -spread
    return per_hr_to_bps_8h(directional) >= min_spread_bps_8h


# ---------------------------------------------------------------------------
# Payload parsing (pure — unit-tested with synthetic venue responses)
# ---------------------------------------------------------------------------


def parse_binance_premium_index(
    rows: Any, coins: list[str]
) -> dict[str, float]:
    """Parse Binance ``/fapi/v1/premiumIndex`` rows -> {hl_coin: per_hr_rate}.

    Each row: ``{"symbol": "BTCUSDT", "lastFundingRate": "0.0001", ...}`` where
    ``lastFundingRate`` is the per-8h rate. Only coins we asked for are returned.
    """
    want = {hl_to_binance(c): c for c in coins}
    want.pop(None, None)
    out: dict[str, float] = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = r.get("symbol")
        hl_coin = want.get(sym)
        if hl_coin is None:
            continue
        try:
            out[hl_coin] = per_hr_from_interval(float(r.get("lastFundingRate")))
        except (TypeError, ValueError):
            continue
    return out


def parse_bybit_tickers(payload: Any, coins: list[str]) -> dict[str, float]:
    """Parse Bybit ``/v5/market/tickers`` payload -> {hl_coin: per_hr_rate}.

    Shape: ``{"result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.0001"}]}}``
    where ``fundingRate`` is the per-interval (8h) rate.
    """
    want = {hl_to_bybit(c): c for c in coins}
    want.pop(None, None)
    out: dict[str, float] = {}
    if not isinstance(payload, dict):
        return out
    items = (((payload.get("result") or {}).get("list")) or [])
    if not isinstance(items, list):
        return out
    for r in items:
        if not isinstance(r, dict):
            continue
        hl_coin = want.get(r.get("symbol"))
        if hl_coin is None:
            continue
        fr = r.get("fundingRate")
        if fr in (None, ""):
            continue
        try:
            out[hl_coin] = per_hr_from_interval(float(fr))
        except (TypeError, ValueError):
            continue
    return out


def merge_xvenue(
    binance: dict[str, float], bybit: dict[str, float]
) -> dict[str, dict[str, float]]:
    """Combine per-venue per-hour rates into the MarketView ``funding_xvenue``
    shape: ``{coin: {"binance": r, "bybit": r}}`` (only present venues)."""
    out: dict[str, dict[str, float]] = {}
    for coin, r in binance.items():
        out.setdefault(coin, {})["binance"] = r
    for coin, r in bybit.items():
        out.setdefault(coin, {})["bybit"] = r
    return out


# ---------------------------------------------------------------------------
# Network fetchers (thin, best-effort, degrade to empty — not unit-tested)
# ---------------------------------------------------------------------------

_BINANCE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BYBIT_URL = "https://api.bybit.com/v5/market/tickers"


def fetch_xvenue_funding(
    coins: list[str], *, timeout: float = 10.0
) -> dict[str, dict[str, float]]:  # pragma: no cover - network
    """Fetch Binance + Bybit funding for ``coins`` and return the
    ``funding_xvenue`` map. Best-effort: any failure yields a partial/empty map
    (the filter fails open), so this never breaks a tick."""
    import httpx

    binance: dict[str, float] = {}
    bybit: dict[str, float] = {}
    try:
        with httpx.Client(timeout=timeout) as cli:
            try:
                rows = cli.get(_BINANCE_URL).json()
                binance = parse_binance_premium_index(rows, coins)
            except Exception as e:  # noqa: BLE001
                log.debug("binance funding fetch failed: %s", e)
            try:
                payload = cli.get(
                    _BYBIT_URL, params={"category": "linear"}
                ).json()
                bybit = parse_bybit_tickers(payload, coins)
            except Exception as e:  # noqa: BLE001
                log.debug("bybit funding fetch failed: %s", e)
    except Exception as e:  # noqa: BLE001
        log.debug("xvenue funding fetch failed: %s", e)
    return merge_xvenue(binance, bybit)
