"""WebSocket market-state tests — message handling + snapshot IO (no socket)."""

from __future__ import annotations

import time

from hl_bot.ingest.ws import MarketState, load_fresh_snapshot, write_snapshot


def test_allmids_updates_mids():
    st = MarketState()
    st.apply_message({"channel": "allMids", "data": {"mids": {"BTC": "64000.5", "ETH": "3000"}}})
    assert st.mids["BTC"] == 64000.5
    assert st.mids["ETH"] == 3000.0


def test_l2book_sets_book_top_and_mid():
    st = MarketState()
    st.apply_message({"channel": "l2Book", "data": {
        "coin": "BTC",
        "levels": [[{"px": "63999", "sz": "1"}], [{"px": "64001", "sz": "1"}]],
    }})
    assert st.book_top["BTC"] == (63999.0, 64001.0)
    assert st.mids["BTC"] == 64000.0


def test_active_asset_ctx_sets_funding_oi_vol():
    st = MarketState()
    st.apply_message({"channel": "activeAssetCtx", "data": {
        "coin": "BTC", "ctx": {"funding": "0.0001", "openInterest": "1234", "dayNtlVlm": "5e8"},
    }})
    assert st.funding["BTC"] == 0.0001
    assert st.open_interest["BTC"] == 1234.0
    assert st.day_ntl_vlm["BTC"] == 5e8


def test_trades_and_liquidation_filter():
    st = MarketState()
    now = int(time.time() * 1000)
    st.apply_message({"channel": "trades", "data": [
        {"coin": "BTC", "side": "A", "px": "64000", "sz": "0.5", "time": now, "liquidation": True},
        {"coin": "BTC", "side": "B", "px": "64000", "sz": "0.1", "time": now},  # normal
    ]})
    liqs = st.recent_liquidations(window_s=300)
    assert len(liqs) == 1
    assert liqs[0]["notional_usd"] == 64000 * 0.5


def test_to_market_view_and_unknown_channel_ignored():
    st = MarketState()
    st.apply_message({"channel": "allMids", "data": {"mids": {"SOL": "150"}}})
    st.apply_message({"channel": "subscriptionResponse", "data": {}})  # ignored
    v = st.to_market_view()
    assert v.mids["SOL"] == 150.0


def test_snapshot_roundtrip_and_staleness(tmp_path):
    st = MarketState()
    st.apply_message({"channel": "allMids", "data": {"mids": {"BTC": "64000"}}})
    st.apply_message({"channel": "activeAssetCtx",
                      "data": {"coin": "BTC", "ctx": {"funding": "0.0002"}}})
    p = tmp_path / "snap.json"
    write_snapshot(st, p)

    fresh = load_fresh_snapshot(p, max_age_s=60)
    assert fresh is not None
    assert fresh.mids["BTC"] == 64000.0
    assert fresh.funding["BTC"] == 0.0002

    # stale snapshot -> None (tick falls back to REST)
    assert load_fresh_snapshot(p, max_age_s=-1) is None
    # missing file -> None
    assert load_fresh_snapshot(tmp_path / "nope.json") is None
