"""WebSocket market-state tests — message handling + snapshot IO (no socket)."""

from __future__ import annotations

import time

import pytest

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


def test_user_fills_captured_and_windowed():
    st = MarketState()
    now = int(time.time() * 1000)
    st.apply_message({"channel": "userFills", "data": {"isSnapshot": True, "fills": [
        {"hash": "0xh1", "tid": 1, "time": now, "coin": "BTC", "side": "B",
         "px": "64000", "sz": "0.01", "cloid": "0xabc1"},
        {"hash": "0xh2", "tid": 2, "time": now - 3600 * 1000, "coin": "ETH", "side": "A",
         "px": "3000", "sz": "1", "cloid": "0xabc2"},  # stale, outside default window
        {"side": "B"},  # malformed (no hash/tid) -> dropped
    ]}})
    recent = st.recent_user_fills(window_s=1800)
    assert [f["coin"] for f in recent] == ["BTC"]
    # all valid fills retained on the deque regardless of window
    assert len(st.user_fills) == 2


def test_user_fills_snapshot_roundtrip(tmp_path):
    st = MarketState()
    now = int(time.time() * 1000)
    st.apply_message({"channel": "userFills", "data": {"fills": [
        {"hash": "0xh1", "tid": 7, "time": now, "coin": "BTC", "side": "B",
         "px": "64000", "sz": "0.01", "cloid": "0xabc1"},
    ]}})
    p = tmp_path / "snap.json"
    write_snapshot(st, p)
    fresh = load_fresh_snapshot(p, max_age_s=60)
    assert fresh is not None
    fills = fresh.extra["user_fills"]
    assert len(fills) == 1
    assert fills[0]["hash"] == "0xh1" and fills[0]["cloid"] == "0xabc1"


def test_to_market_view_and_unknown_channel_ignored():
    st = MarketState()
    st.apply_message({"channel": "allMids", "data": {"mids": {"SOL": "150"}}})
    st.apply_message({"channel": "subscriptionResponse", "data": {}})  # ignored
    v = st.to_market_view()
    assert v.mids["SOL"] == 150.0


def _invoke_ws_command(monkeypatch, tmp_path):
    """Run `hlbot ws` with run_ws faked out; return the kwargs it was wired with."""
    from typer.testing import CliRunner

    import hl_bot.ingest.ws as ws_mod
    from hl_bot.cli.main import app

    calls: dict = {}

    def fake_run_ws(coins, snapshot_path, **kwargs):
        calls["coins"] = coins
        calls.update(kwargs)

    monkeypatch.setattr(ws_mod, "run_ws", fake_run_ws)
    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "t.sqlite"))
    res = CliRunner().invoke(app, ["ws"])
    assert res.exit_code == 0, res.output
    return calls


def test_ws_command_subscribes_user_fills(monkeypatch, tmp_path):
    # B10c: the deployed ws service must watch the account the tick trades,
    # else maker-fill detection silently degrades to next-REST-poll latency.
    monkeypatch.delenv("HL_VAULT_ADDRESS", raising=False)
    monkeypatch.setenv("HL_TRADER_ADDRESS", "0x" + "c" * 40)
    calls = _invoke_ws_command(monkeypatch, tmp_path)
    assert calls["user_address"] == "0x" + "c" * 40
    assert calls["coins"] == ["BTC", "ETH", "SOL", "HYPE"]


def test_ws_command_user_fills_follow_vault(monkeypatch, tmp_path):
    # With a vault live, fills land on the vault — userFills must follow it.
    monkeypatch.setenv("HL_TRADER_ADDRESS", "0x" + "c" * 40)
    monkeypatch.setenv("HL_VAULT_ADDRESS", "0x" + "d" * 40)
    calls = _invoke_ws_command(monkeypatch, tmp_path)
    assert calls["user_address"] == "0x" + "d" * 40


class _FakeInfo:
    """Stands in for hyperliquid.info.Info — records subscriptions + disconnect."""

    instances: list[_FakeInfo] = []

    def __init__(self, base_url, skip_ws=False):
        self.base_url = base_url
        self.subscriptions: list[dict] = []
        self.disconnected = False
        _FakeInfo.instances.append(self)

    def subscribe(self, sub, cb):
        self.subscriptions.append(sub)

    def disconnect_websocket(self):
        self.disconnected = True


def _patch_fake_info(monkeypatch):
    import hyperliquid.info as hl_info

    _FakeInfo.instances.clear()
    monkeypatch.setattr(hl_info, "Info", _FakeInfo)


def test_run_ws_disconnects_on_duration_exit(monkeypatch, tmp_path):
    # B10d: the SDK ws thread is non-daemon — a bounded run must disconnect
    # or the process never exits (`hlbot ws --seconds N` hung until killed).
    from hl_bot.ingest.ws import run_ws

    _patch_fake_info(monkeypatch)
    run_ws(["BTC"], tmp_path / "snap.json", duration_s=0, user_address="0x" + "a" * 40)

    (info,) = _FakeInfo.instances
    assert info.disconnected
    subs = {(s["type"], s.get("coin"), s.get("user")) for s in info.subscriptions}
    assert subs == {
        ("allMids", None, None),
        ("userFills", None, "0x" + "a" * 40),
        ("l2Book", "BTC", None),
        ("trades", "BTC", None),
        ("activeAssetCtx", "BTC", None),
    }


def test_run_ws_disconnects_even_when_loop_raises(monkeypatch, tmp_path):
    from hl_bot.ingest.ws import run_ws

    _patch_fake_info(monkeypatch)

    def _boom(self, sub, cb):
        raise RuntimeError("boom")

    monkeypatch.setattr(_FakeInfo, "subscribe", _boom)
    with pytest.raises(RuntimeError):
        run_ws(["BTC"], tmp_path / "snap.json", duration_s=0)
    (info,) = _FakeInfo.instances
    assert info.disconnected


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
