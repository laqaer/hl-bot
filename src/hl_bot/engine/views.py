"""Market view construction: REST baseline + WS overlay + signal enrichment.

Moved out of the CLI so the long-running runner and the one-shot tick share
one implementation. Prefers the WS snapshot (sub-second mids, L2 book, real
liquidations) and falls back to REST for anything the socket hasn't covered.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from ..agents.base import MarketView
from ..agents.runtime import fetch_market_view

log = logging.getLogger(__name__)


def build_view(
    api_url: str,
    *,
    ws_snapshot_path: str | None = None,
    universe_size: int = 20,
    max_workers: int = 8,
) -> MarketView:
    """Full market view for one cycle: REST fetch, WS overlay, enrichment."""
    view = fetch_market_view(api_url, [])
    enrich_view(view, api_url, view.extra.get("day_ntl_vlm", {}),
                universe_size=universe_size, max_workers=max_workers)
    overlay_ws_snapshot(view, ws_snapshot_path)
    return view


def overlay_ws_snapshot(view: MarketView, ws_snapshot_path: str | None = None) -> bool:
    """Overlay a fresh WS snapshot if available (purely additive; REST is the
    fallback). Returns whether an overlay happened."""
    path = ws_snapshot_path or os.environ.get("HLBOT_WS_SNAPSHOT")
    if not path:
        return False
    from ..ingest.ws import load_fresh_snapshot
    snap = load_fresh_snapshot(path, max_age_s=30.0)
    if snap is None:
        return False
    view.mids.update(snap.mids)
    view.funding.update(snap.funding)
    # Keep the funding SIGNAL in sync with the freshest funding: enrich_view()
    # ran BEFORE this overlay and copied the (now stale) REST funding into
    # extra["funding_hourly"]; funding-threshold agents read that field, so
    # re-mirror it here or they gate on stale rates in WS-enabled runs.
    view.extra["funding_hourly"] = dict(view.funding)
    if snap.book_top:
        view.book_top.update(snap.book_top)
    book_imb = snap.extra.get("book_imb") or {}
    if book_imb:
        view.extra["book_imb"] = dict(book_imb)
    liqs = snap.extra.get("liquidations") or []
    if liqs:
        view.extra["liquidations"] = liqs
    return True


def enrich_view(
    view: MarketView,
    api_url: str,
    vol: dict[str, float],
    *,
    universe_size: int = 20,
    max_workers: int = 8,
) -> None:
    """Augment a MarketView with 1h/5m candle signals (top-vol coins), spot mids,
    liquidations.

    ``universe_size`` is the number of top-by-24h-volume coins to compute candle
    vwap/sigma for — the breadth ceiling for ``dislocation_reversion`` /
    ``funding_crowding_fade`` (each still gated by its own volume floor, so a
    wider universe never forces an agent into illiquid coins; it just lets the
    forward soak see more dislocations and clear G0 faster). The per-coin candle
    fetches (2 ``candleSnapshot`` calls each) are issued concurrently with a
    ``max_workers``-bounded pool so widening breadth doesn't blow the cycle's
    time budget; ``max_workers<=1`` keeps the old serial behaviour.
    """
    # ---- top-by-volume universe ----
    top = sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:max(0, universe_size)]
    top_coins = [c for c, _ in top]

    candles_1h: dict[str, dict] = {}
    candles_5m: dict[str, dict] = {}
    closes_by_coin: dict[str, list[float]] = {}
    spot_mids: dict[str, float] = {}
    liquidations: list[dict] = []

    def _vwap_sigma(cli, coin, interval, span_ms):
        """(stats, closes) for ``coin`` over the last 60 ``interval`` bars, or
        (None, None). stats = {vwap, sigma, n}; sigma is the std of closes."""
        end_ms = int(time.time() * 1000)
        cs = cli.post(api_url + "/info", json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": end_ms - span_ms, "endTime": end_ms},
        }).json() or []
        if not isinstance(cs, list) or len(cs) < 10:
            return None, None
        pxs, vols = [], []
        for k in cs:
            try:
                c_px, c_vol = float(k.get("c", 0)), float(k.get("v", 0))
                if c_px > 0:
                    pxs.append(c_px)
                    vols.append(c_vol)
            except (TypeError, ValueError):
                continue
        if len(pxs) < 10:
            return None, None
        tot_vol = sum(vols)
        vwap = sum(p * v for p, v in zip(pxs, vols, strict=False)) / tot_vol if tot_vol > 0 else sum(pxs) / len(pxs)
        mean = sum(pxs) / len(pxs)
        sigma = (sum((p - mean) ** 2 for p in pxs) / len(pxs)) ** 0.5
        return {"vwap": vwap, "sigma": sigma, "n": len(pxs)}, pxs

    def _fetch_coin(cli, coin):
        """Both candle signals for one coin; per-coin failures are isolated so a
        single bad coin never drops the rest of the universe."""
        out: dict = {}
        try:
            # twap_mr family: 60×1m = 1h window (candles_1h).
            stats1, closes1 = _vwap_sigma(cli, coin, "1m", 60 * 60_000)
            if stats1:
                out["1h"] = (stats1, closes1)
            # dislocation_reversion: 60×5m = 5h window (candles_5m). MUST match
            # its backtest basis (interval 5m, vwap_window 60) or live trades a
            # different signal than confirmed (the twap_mr lesson).
            stats5, _ = _vwap_sigma(cli, coin, "5m", 60 * 5 * 60_000)
            if stats5:
                out["5m"] = stats5
        except Exception:  # noqa: BLE001
            return coin, {}
        return coin, out

    with httpx.Client(timeout=15) as cli:
        if max_workers > 1 and len(top_coins) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(max_workers, len(top_coins))) as ex:
                results = list(ex.map(lambda c: _fetch_coin(cli, c), top_coins))
        else:
            results = [_fetch_coin(cli, c) for c in top_coins]
        for coin, out in results:
            if "1h" in out:
                candles_1h[coin] = out["1h"][0]
                closes_by_coin[coin] = out["1h"][1]
            if "5m" in out:
                candles_5m[coin] = out["5m"]

        # Spot mids for BTC/ETH/SOL. HL spot pairs use wrapped tokens
        # (UBTC/USDC=@142, UETH/USDC=@151, USOL/USDC=@156) and the midPx is
        # quoted in scaled native units. We use allMids @N indices and scale
        # against the perp mid to detect basis: skip pair if it would produce
        # a clearly nonsensical (>5%) basis (means we don't have a clean spot).
        try:
            spot = cli.post(api_url + "/info", json={"type": "spotMetaAndAssetCtxs"}).json()
            if isinstance(spot, list) and len(spot) == 2:
                meta = spot[0] or {}
                ctxs = spot[1] or []
                universe = meta.get("universe", []) or []
                tokens = meta.get("tokens", []) or []
                name_by_token = {t.get("index"): t.get("name") for t in tokens}
                # token szDecimals required to normalize price
                wei_by_token = {t.get("index"): int(t.get("weiDecimals", 0) or 0) for t in tokens}
                for u, c in zip(universe, ctxs, strict=False):
                    pair_tokens = u.get("tokens", [])
                    if len(pair_tokens) < 2:
                        continue
                    base_idx = pair_tokens[0]
                    base_name = name_by_token.get(base_idx)
                    quote_name = name_by_token.get(pair_tokens[1])
                    if quote_name != "USDC":
                        continue
                    norm = None
                    if base_name in ("UBTC", "UETH", "USOL"):
                        norm = base_name[1:]   # strip leading 'U'
                    elif base_name in ("BTC", "ETH", "SOL"):
                        norm = base_name
                    if norm not in ("BTC", "ETH", "SOL"):
                        continue
                    try:
                        raw_mid = float(c.get("midPx") or 0)
                    except (TypeError, ValueError):
                        raw_mid = 0
                    if raw_mid <= 0:
                        continue
                    # USDC weiDecimals=8 (standard). base wei from token meta.
                    base_wei = wei_by_token.get(base_idx, 8)
                    quote_wei = 8  # USDC
                    scaled_mid = raw_mid * (10 ** (base_wei - quote_wei))
                    # only adopt if scaled_mid is within 5% of perp mid (sanity)
                    perp_mid = view.mids.get(norm)
                    if (
                        perp_mid and scaled_mid > 0
                        and 0.5 < scaled_mid / perp_mid < 1.5
                        and ((base_name or "").startswith("U") or norm not in spot_mids)
                    ):
                        # Prefer wrapped (U-prefixed) over plain if both present.
                        spot_mids[norm] = scaled_mid
        except Exception:  # noqa: BLE001
            pass

        # recent liquidations (best-effort; endpoint may not exist publicly)
        try:
            ev = cli.post(api_url + "/info", json={"type": "liquidations"}).json()
            if isinstance(ev, list):
                for e in ev:
                    try:
                        coin = e.get("coin")
                        sz = float(e.get("sz") or 0)
                        px = float(e.get("px") or 0)
                        if coin and sz > 0 and px > 0:
                            liquidations.append({
                                "coin": coin,
                                "side": e.get("side"),
                                "notional_usd": sz * px,
                                "ts_ms": int(e.get("time") or 0),
                            })
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001
            pass

    view.extra["candles_1h"] = candles_1h
    view.extra["candles_5m"] = candles_5m
    view.extra["closes"] = closes_by_coin
    view.extra["spot_mids"] = spot_mids
    view.extra["liquidations"] = liquidations
    # Unscaled 1h funding as a signal, mirroring the backtest frame's
    # funding_hourly (live view.funding is already the 1h rate). Lets
    # funding-threshold agents read identical units in paper/live and backtest.
    view.extra["funding_hourly"] = dict(view.funding)
    # Also surface each spot mid under a "<coin>-SPOT" key in view.mids so the
    # spot-perp carry (S4) agent prices its spot leg identically in paper and
    # backtest (the backtest puts spot in mids[-SPOT] too). extra["spot_mids"]
    # stays the canonical source for basis.py and friends.
    for coin, smid in spot_mids.items():
        view.mids[f"{coin}-SPOT"] = smid
