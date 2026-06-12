"""Tests for the shared live/paper tick-harness pieces in ``runtime``.

These functions were extracted from the inlined, untested ``femr_tick`` preamble
(REVIEW M3 / B12) so the live path shares tested code with the paper path:

- ``positions_from_clearinghouse`` — pure parse of HL ``clearinghouseState`` into
  the bot's position-dict shape, skipping malformed entries.
- ``reconcile_agents`` — per-agent stale-ownership reconcile against HL truth,
  returning only the agents that had something cleared.
- ``apply_allocator_caps`` — allocate the 7d split, resolve the layered risk
  rule, and write the binding caps onto each agent's cfg.
- ``overlay_ws_snapshot`` — additive merge of a fresh WS snapshot onto the live
  REST view, enabling the real liquidations feed.
- ``fetch_account_state`` — the clearinghouse/spot account fetch + derived
  sizing values (perp value, spot USDC, unified portfolio, withdrawable).
- ``load_agent_overrides`` / ``build_roster`` — auto-tuner override loading
  (every failure mode degrades to built-in defaults) and the canonical agent
  roster with defaults + overrides merged per agent.
- ``enrich_view`` / ``build_tick_view`` — VWAP/σ + spot + 15m-feed enrichment
  and the composed fetch→enrich→WS-overlay view pipeline both ``run_tick``
  (paper) and ``femr_tick`` (live) decide on.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.runtime import (
    apply_allocator_caps,
    build_roster,
    build_tick_view,
    classify_position_ownership,
    closes_15m_bars,
    enrich_view,
    fetch_account_state,
    load_agent_overrides,
    overlay_ws_snapshot,
    positions_from_clearinghouse,
    reconcile_agents,
    resolve_vwap_window,
)
from hl_bot.db.schema import init_db
from hl_bot.risk.scaling import NotionalCap


def _risk_cap(max_total: float, max_per_pos: float) -> NotionalCap:
    return NotionalCap(
        max_total_notional=max_total,
        max_per_position_notional=max_per_pos,
        portfolio_value=max_per_pos,
        avg_account_value=max_per_pos,
        multiplier=5.0,
        per_position_multiplier=1.0,
        ceiling_notional=None,
        lookback_days=7,
        sample_count=1,
        source="test",
    )


def _agent(name: str, cfg=None):
    return SimpleNamespace(name=name, cfg=cfg) if cfg is not None else SimpleNamespace(name=name)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def test_positions_from_clearinghouse_parses_fields():
    st = {
        "assetPositions": [
            {"position": {
                "coin": "BTC", "szi": "0.5", "entryPx": "100", "positionValue": "50",
                "unrealizedPnl": "1.5", "liquidationPx": "80",
                "leverage": {"value": 3}, "marginUsed": "10",
            }},
        ]
    }
    out = positions_from_clearinghouse(st)
    assert len(out) == 1
    p = out[0]
    assert p["coin"] == "BTC"
    assert p["szi"] == 0.5
    assert p["entry_px"] == 100.0
    assert p["leverage"] == 3
    assert p["margin_used"] == 10.0


def test_positions_from_clearinghouse_handles_empty_and_missing():
    assert positions_from_clearinghouse({}) == []
    assert positions_from_clearinghouse({"assetPositions": None}) == []
    # Missing leverage dict and numeric fields default rather than crash.
    out = positions_from_clearinghouse({"assetPositions": [{"position": {"coin": "ETH"}}]})
    assert out == [{
        "coin": "ETH", "szi": 0.0, "entry_px": 0.0, "position_value": 0.0,
        "unrealized_pnl": 0.0, "liquidation_px": 0.0, "leverage": None,
        "margin_used": 0.0,
    }]


def test_reconcile_agents_clears_stale_per_agent(conn):
    # Two agents each "own" a coin per their decision log…
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    log_decision(conn, Decision(agent="b", action="place", coin="ETH", is_paper=False))
    # …but HL truth shows only BTC is live. ETH (agent b) is stale.
    live = [{"coin": "BTC", "szi": 0.5}]
    out = reconcile_agents(conn, live, ["a", "b"])
    assert out == {"b": ["ETH"]}
    # The reconcile wrote a synthetic flatten so b no longer owns ETH.
    from hl_bot.exec.orders import bot_owned_coins
    assert bot_owned_coins(conn, "b") == set()
    assert bot_owned_coins(conn, "a") == {"BTC"}


def test_reconcile_agents_noop_when_all_present(conn):
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    live = [{"coin": "BTC", "szi": 0.5}]
    assert reconcile_agents(conn, live, ["a"]) == {}


def test_apply_allocator_caps_honors_explicit_cfg_and_mutates(conn):
    # Empty fills -> cold-start: allocator floors each agent at min_alloc (50).
    # An explicit sub-legacy total (30) is honored; per-trade (10) is preserved.
    cfg = SimpleNamespace(max_total_notional=30.0, max_notional_per_trade=10.0)
    agents = [_agent("a", cfg)]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=500.0, max_per_pos=100.0))
    # min(alloc=50, approved_total=30, per_pos=100) = 30.
    assert out.effective_caps["a"] == 30.0
    assert out.effective_order_caps["a"] == 10.0
    # The agent's cfg is mutated in place with the binding caps.
    assert cfg.max_total_notional == 30.0
    assert cfg.max_notional_per_trade == 10.0


def test_apply_allocator_caps_caps_at_per_position_ceiling(conn):
    # No configured cap -> dynamic 1x per-position ceiling binds. alloc floors at
    # 50, per_pos ceiling is 40, so total = min(50, 40) = 40.
    cfg = SimpleNamespace(max_total_notional=float("inf"), max_notional_per_trade=float("inf"))
    agents = [_agent("a", cfg)]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=200.0, max_per_pos=40.0))
    assert out.effective_caps["a"] == 40.0
    assert cfg.max_total_notional == 40.0


def test_apply_allocator_caps_agent_without_cfg_left_untouched(conn):
    # An agent with no cfg keeps its raw alloc in effective_caps and is not
    # mutated (no cfg to write).
    agents = [_agent("a")]
    out = apply_allocator_caps(conn, agents, _risk_cap(max_total=500.0, max_per_pos=100.0))
    assert out.effective_caps["a"] == out.allocs["a"]
    assert "a" not in out.effective_order_caps
    assert not hasattr(agents[0], "cfg")


def test_classify_position_ownership_splits_bot_and_manual(conn):
    # Two roster agents own BTC / ETH; SOL is live but owned by nobody -> manual.
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    log_decision(conn, Decision(agent="b", action="place", coin="ETH", is_paper=False))
    live = [{"coin": "BTC"}, {"coin": "ETH"}, {"coin": "SOL"}]
    out = classify_position_ownership(conn, live, ["a", "b"])
    assert out.owned_by_agent == {"a": {"BTC"}, "b": {"ETH"}}
    assert out.owned_all == {"BTC", "ETH"}
    assert out.manual_coins == ["SOL"]


def test_classify_position_ownership_filtered_agent_coin_is_manual(conn):
    # 'b' owns ETH per its log, but is NOT in the roster (e.g. not promoted to
    # live) -> ETH must show as manual, not bot-owned. Order is preserved.
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    log_decision(conn, Decision(agent="b", action="place", coin="ETH", is_paper=False))
    live = [{"coin": "ETH"}, {"coin": "BTC"}]
    out = classify_position_ownership(conn, live, ["a"])
    assert out.owned_all == {"BTC"}
    assert out.manual_coins == ["ETH"]


def test_classify_position_ownership_flatten_drops_ownership(conn):
    # A place then flatten means the agent no longer owns the coin -> manual.
    log_decision(conn, Decision(agent="a", action="place", coin="BTC", is_paper=False))
    log_decision(conn, Decision(agent="a", action="flatten", coin="BTC", is_paper=False))
    live = [{"coin": "BTC"}]
    out = classify_position_ownership(conn, live, ["a"])
    assert out.owned_all == set()
    assert out.manual_coins == ["BTC"]


def test_overlay_ws_snapshot_none_is_noop():
    view = MarketView(ts_ms=0, mids={"BTC": 100.0}, funding={"BTC": 0.0001})
    out = overlay_ws_snapshot(view, None)
    assert out.applied is False
    assert out.n_mids == 0 and out.n_liqs == 0
    # REST view untouched; no liquidations feed flag injected.
    assert view.mids == {"BTC": 100.0}
    assert "liquidations_feed" not in view.extra


def test_overlay_ws_snapshot_merges_and_enables_feed():
    view = MarketView(
        ts_ms=0,
        mids={"BTC": 100.0, "ETH": 50.0},
        funding={"BTC": 0.0001},
        book_top={"BTC": (99.0, 101.0)},
    )
    snap = MarketView(
        ts_ms=1,
        mids={"BTC": 100.5},  # fresher BTC mid overrides; ETH preserved
        funding={"BTC": 0.0002, "SOL": 0.0003},
        book_top={"SOL": (9.9, 10.1)},
        extra={"liquidations": [{"coin": "BTC"}, {"coin": "ETH"}]},
    )
    out = overlay_ws_snapshot(view, snap)
    assert out.applied is True
    assert out.n_mids == 1 and out.n_liqs == 2
    assert view.mids == {"BTC": 100.5, "ETH": 50.0}
    assert view.funding == {"BTC": 0.0002, "SOL": 0.0003}
    assert view.book_top == {"BTC": (99.0, 101.0), "SOL": (9.9, 10.1)}
    assert view.extra["liquidations"] == [{"coin": "BTC"}, {"coin": "ETH"}]
    assert view.extra["liquidations_feed"] is True


def test_overlay_ws_snapshot_empty_liqs_still_enables_feed():
    # A fresh snapshot with no liquidations is a calm market, NOT a broken feed:
    # the feed flag is still set so liq_cascade entries are enabled.
    view = MarketView(ts_ms=0, mids={"BTC": 100.0})
    snap = MarketView(ts_ms=1, mids={"BTC": 100.0}, extra={})
    out = overlay_ws_snapshot(view, snap)
    assert out.applied is True
    assert out.n_liqs == 0
    assert view.extra["liquidations"] == []
    assert view.extra["liquidations_feed"] is True


# ---------------------------------------------------------------------------
# fetch_account_state (B12g): the femr_tick account/risk-sizing preamble
# ---------------------------------------------------------------------------


class _FakeAcctClient:
    """Serves canned /info payloads keyed by request type; can fail per type."""

    payloads: dict = {}
    fail: set = set()
    requests: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        t = (json or {}).get("type")
        _FakeAcctClient.requests.append(json)
        if t in _FakeAcctClient.fail:
            import httpx

            raise httpx.ConnectError("boom")
        return _Resp(_FakeAcctClient.payloads.get(t))


def _fetch_account(monkeypatch, payloads, fail=()):
    import httpx

    _FakeAcctClient.payloads = payloads
    _FakeAcctClient.fail = set(fail)
    _FakeAcctClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeAcctClient)
    return fetch_account_state("http://fake", "0xabc")


def test_fetch_account_state_parses_and_unifies(monkeypatch):
    st = {"marginSummary": {"accountValue": "123.45"}, "withdrawable": "67.8"}
    spot = {"balances": [{"coin": "HYPE", "total": "999"},
                         {"coin": "USDC", "total": "10.55"}]}
    out = _fetch_account(monkeypatch, {
        "clearinghouseState": st, "spotClearinghouseState": spot,
    })
    assert out.account_value == pytest.approx(123.45)
    assert out.spot_usdc == pytest.approx(10.55)
    assert out.portfolio_value == pytest.approx(134.0), "unified = perp + spot USDC"
    assert out.withdrawable == pytest.approx(67.8)
    # Raw payloads ride along for the position parse and guardrails.
    assert out.clearinghouse is st
    assert out.spot_clearinghouse is spot
    # Both /info calls address the configured trader.
    assert [r["type"] for r in _FakeAcctClient.requests] == [
        "clearinghouseState", "spotClearinghouseState"]
    assert all(r["user"] == "0xabc" for r in _FakeAcctClient.requests)


def test_fetch_account_state_spot_outage_tightens_not_aborts(monkeypatch):
    # Spot endpoint down -> spot USDC counts as 0, so portfolio value (and hence
    # the notional caps) only shrinks. The tick must NOT abort.
    st = {"marginSummary": {"accountValue": "50"}, "withdrawable": "5"}
    out = _fetch_account(monkeypatch, {"clearinghouseState": st},
                         fail={"spotClearinghouseState"})
    assert out.spot_clearinghouse == {}
    assert out.spot_usdc == 0.0
    assert out.portfolio_value == pytest.approx(50.0)


def test_fetch_account_state_perp_failure_propagates(monkeypatch):
    # The perp clearinghouse is the tick's ground truth: a tick must never size
    # risk blind, so the HTTP error propagates instead of degrading to zeros.
    import httpx

    with pytest.raises(httpx.HTTPError):
        _fetch_account(monkeypatch, {}, fail={"clearinghouseState"})


def test_fetch_account_state_null_and_malformed_fields_zero(monkeypatch):
    # A null payload (.json() -> None) and a malformed withdrawable both degrade
    # to zeros rather than crashing the tick.
    out = _fetch_account(monkeypatch, {
        "clearinghouseState": {"withdrawable": "n/a"},
        "spotClearinghouseState": None,
    })
    assert out.account_value == 0.0
    assert out.spot_usdc == 0.0
    assert out.portfolio_value == 0.0
    assert out.withdrawable == 0.0


# ---------------------------------------------------------------------------
# resolve_vwap_window (B-WIN2): the live VWAP window is operator-flippable
# ---------------------------------------------------------------------------


def test_resolve_vwap_window_precedence():
    assert resolve_vwap_window() == 60, "no inputs -> historical default"
    assert resolve_vwap_window(0, {"HLBOT_VWAP_WINDOW": "240"}) == 240, "env used when no CLI value"
    assert resolve_vwap_window(120, {"HLBOT_VWAP_WINDOW": "240"}) == 120, "explicit CLI wins over env"


def test_resolve_vwap_window_rejects_garbage():
    # A typo'd env or absurd CLI value must never silence the signal: fall
    # through to the next source instead of propagating a broken window.
    assert resolve_vwap_window(0, {"HLBOT_VWAP_WINDOW": "4h"}) == 60
    assert resolve_vwap_window(0, {"HLBOT_VWAP_WINDOW": "-5"}) == 60
    assert resolve_vwap_window(0, {"HLBOT_VWAP_WINDOW": "1"}) == 60
    assert resolve_vwap_window(1, {"HLBOT_VWAP_WINDOW": "240"}) == 240, "sub-floor CLI falls to env"
    assert resolve_vwap_window(-3, {}) == 60


# ---------------------------------------------------------------------------
# enrich_view honors vwap_window and matches the backtester's math (B-WIN2)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHlClient:
    """Serves canned 1m candles for any candleSnapshot; empty otherwise."""

    candles: list[dict] = []
    requests: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        if (json or {}).get("type") == "candleSnapshot":
            _FakeHlClient.requests.append(json["req"])
            return _Resp(list(_FakeHlClient.candles))
        return _Resp([])


def _enrich_with_window(monkeypatch, candles, window):
    import httpx

    _FakeHlClient.candles = candles
    _FakeHlClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeHlClient)
    view = MarketView(ts_ms=0, mids={"TST": 100.0})
    enrich_view(view, "http://fake", {"TST": 1e9}, vwap_window=window)
    return view


def test_enrich_view_window_drives_fetch_span_and_math(monkeypatch):
    from hl_bot.backtest.data import rolling_vwap_sigma

    window = 240
    candles = [{"t": i * 60_000, "c": 100.0 + (i % 7), "v": 1.0 + i % 3}
               for i in range(window)]
    view = _enrich_with_window(monkeypatch, candles, window)

    req = _FakeHlClient.requests[0]
    assert req["endTime"] - req["startTime"] == window * 60_000, "fetch span follows the window"

    pxs = [k["c"] for k in candles]
    vols = [k["v"] for k in candles]
    want_vwap, want_sigma = rolling_vwap_sigma(pxs, vols, window)
    got = view.extra["candles_1h"]["TST"]
    assert got["vwap"] == pytest.approx(want_vwap)
    assert got["sigma"] == pytest.approx(want_sigma)
    assert got["n"] == window
    assert view.extra["closes"]["TST"] == pxs[-window:], "closes = the window slice, like backtest frames"


def test_enrich_view_skips_coin_with_too_few_bars(monkeypatch):
    # rolling_vwap_sigma's floor (window//2) governs, matching backtest warmup
    # semantics: a thin/new listing yields no vwap entry rather than a noisy one.
    candles = [{"t": i * 60_000, "c": 100.0, "v": 1.0} for i in range(10)]
    view = _enrich_with_window(monkeypatch, candles, 60)
    assert "TST" not in view.extra["candles_1h"]
    assert "TST" not in view.extra["closes"]


# ---------------------------------------------------------------------------
# 15m closes feed for long-horizon channel agents (B-EDGE2a)
# ---------------------------------------------------------------------------


def test_closes_15m_bars_roster_scan():
    from hl_bot.agents.breakout import BreakoutAgent

    on = BreakoutAgent(config={"lookback_bars": 384, "exit_lookback_bars": 96,
                               "closes_key": "closes_15m"})
    off = BreakoutAgent(config={"lookback_bars": 999})  # default key: not a consumer
    assert closes_15m_bars([]) == 0
    assert closes_15m_bars([off, SimpleNamespace(name="no_cfg")]) == 0
    assert closes_15m_bars([off, on]) == 385, "longest channel + in-progress bar"
    # an exit channel longer than the entry channel still sizes the feed
    long_exit = BreakoutAgent(config={"lookback_bars": 4, "exit_lookback_bars": 500,
                                      "closes_key": "closes_15m"})
    assert closes_15m_bars([on, long_exit]) == 501


class _FakeHlClient15m(_FakeHlClient):
    """Serves 1m and 15m candleSnapshots from separate canned sets."""

    candles_15m: list[dict] = []

    def post(self, url, json=None):
        req = (json or {}).get("req") or {}
        if (json or {}).get("type") == "candleSnapshot" and req.get("interval") == "15m":
            _FakeHlClient.requests.append(req)
            return _Resp(list(_FakeHlClient15m.candles_15m))
        return super().post(url, json=json)


def test_enrich_view_fetches_15m_closes_only_when_asked(monkeypatch):
    import httpx

    _FakeHlClient.candles = [{"t": i * 60_000, "c": 100.0, "v": 1.0} for i in range(60)]
    _FakeHlClient15m.candles_15m = [{"t": i * 900_000, "c": 200.0 + i, "v": 1.0}
                                    for i in range(40)]
    _FakeHlClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeHlClient15m)

    view = MarketView(ts_ms=0, mids={"TST": 100.0})
    enrich_view(view, "http://fake", {"TST": 1e9}, vwap_window=60, closes_15m_bars=33)
    reqs_15m = [r for r in _FakeHlClient.requests if r["interval"] == "15m"]
    assert len(reqs_15m) == 1
    assert reqs_15m[0]["endTime"] - reqs_15m[0]["startTime"] == 33 * 900_000
    assert view.extra["closes_15m"]["TST"] == [200.0 + i for i in range(7, 40)], \
        "trailing closes_15m_bars closes, in-progress bar last"
    assert view.extra["closes"]["TST"], "1m feed unaffected"

    # default bars=0: no 15m traffic at all (live ticks without breakout pay nothing)
    _FakeHlClient.requests = []
    view2 = MarketView(ts_ms=0, mids={"TST": 100.0})
    enrich_view(view2, "http://fake", {"TST": 1e9}, vwap_window=60)
    assert all(r["interval"] == "1m" for r in _FakeHlClient.requests)
    assert view2.extra["closes_15m"] == {}


# ---------------------------------------------------------------------------
# build_tick_view — the one view pipeline shared by run_tick and femr_tick (B12i)
# ---------------------------------------------------------------------------


class _FakeUniverseClient(_FakeHlClient15m):
    """Adds the REST universe endpoints so build_tick_view runs end-to-end."""

    def post(self, url, json=None):
        t = (json or {}).get("type")
        if t == "allMids":
            return _Resp({"TST": "100.0"})
        if t == "metaAndAssetCtxs":
            return _Resp([
                {"universe": [{"name": "TST"}]},
                [{"funding": "0.0001", "openInterest": "5",
                  "dayNtlVlm": "1000000000"}],
            ])
        return super().post(url, json=json)


def _breakout_15m_consumer():
    # 1 + max(lookback, exit_lookback) = 33 bars of 15m feed
    from hl_bot.agents.breakout import BreakoutAgent

    return BreakoutAgent(config={"lookback_bars": 32, "exit_lookback_bars": 4,
                                 "closes_key": "closes_15m"})


def test_build_tick_view_composes_fetch_enrich_and_window(monkeypatch):
    import httpx

    _FakeHlClient.candles = [{"t": i * 60_000, "c": 100.0 + (i % 5), "v": 1.0}
                             for i in range(240)]
    _FakeHlClient15m.candles_15m = [{"t": i * 900_000, "c": 200.0 + i, "v": 1.0}
                                    for i in range(40)]
    _FakeHlClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeUniverseClient)

    tv = build_tick_view("http://fake", [_breakout_15m_consumer()],
                         env={"HLBOT_VWAP_WINDOW": "240"})

    assert tv.vwap_window == 240, "env window resolved (CLI value 0)"
    assert tv.bars_15m == 33, "15m feed sized by the roster's longest channel"
    assert tv.ws is None, "no HLBOT_WS_SNAPSHOT -> no overlay attempted"
    assert tv.view.mids == {"TST": 100.0}
    assert tv.view.funding == {"TST": 0.0001}
    reqs_1m = [r for r in _FakeHlClient.requests if r["interval"] == "1m"]
    assert reqs_1m[0]["endTime"] - reqs_1m[0]["startTime"] == 240 * 60_000, \
        "enrichment fetch span follows the resolved window"
    assert tv.view.extra["candles_1h"]["TST"]["n"] == 240
    assert len(tv.view.extra["closes_15m"]["TST"]) == 33
    assert tv.view.extra["liquidations_feed"] is False, "REST-only: no real liq feed"


def test_build_tick_view_overlays_fresh_ws_snapshot(monkeypatch, tmp_path):
    import httpx

    _FakeHlClient.candles = [{"t": i * 60_000, "c": 100.0, "v": 1.0} for i in range(60)]
    _FakeHlClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeUniverseClient)

    snap_path = tmp_path / "ws.json"
    snap_path.write_text(json.dumps({
        "updated_ms": int(time.time() * 1000),
        "mids": {"TST": 101.5},
        "funding": {},
        "open_interest": {},
        "day_ntl_vlm": {},
        "book_top": {"TST": [101.4, 101.6]},
        "recent_liquidations": [{"coin": "TST", "px": 99.0}],
        "user_fills": [],
    }))

    tv = build_tick_view("http://fake", [], env={"HLBOT_WS_SNAPSHOT": str(snap_path)})
    assert tv.ws is not None and tv.ws.applied
    assert tv.view.mids["TST"] == 101.5, "fresh WS mid wins over REST"
    assert tv.view.book_top["TST"] == (101.4, 101.6)
    assert tv.view.extra["liquidations_feed"] is True, "WS snapshot IS a real liq feed"
    assert tv.view.extra["liquidations"] == [{"coin": "TST", "px": 99.0}]

    # Stale snapshot: overlay attempted but not applied; REST stays the truth.
    snap_path.write_text(json.dumps({"updated_ms": 1, "mids": {"TST": 999.0}}))
    tv2 = build_tick_view("http://fake", [], env={"HLBOT_WS_SNAPSHOT": str(snap_path)})
    assert tv2.ws is not None and not tv2.ws.applied
    assert tv2.view.mids["TST"] == 100.0
    assert tv2.view.extra["liquidations_feed"] is False


# ---------------------------------------------------------------------------
# load_agent_overrides / build_roster (B12h)


def test_load_agent_overrides_missing_file_is_empty(tmp_path):
    assert load_agent_overrides(tmp_path / "nope.json") == {}


def test_load_agent_overrides_parses_valid_file(tmp_path):
    p = tmp_path / "agent_overrides.json"
    p.write_text('{"femr_v1": {"max_notional_per_trade": 7.5}}')
    assert load_agent_overrides(p) == {"femr_v1": {"max_notional_per_trade": 7.5}}


def test_load_agent_overrides_malformed_json_degrades_to_defaults(tmp_path, caplog):
    p = tmp_path / "agent_overrides.json"
    p.write_text("{not json")
    with caplog.at_level("WARNING"):
        assert load_agent_overrides(p) == {}
    assert "using built-in defaults" in caplog.text


def test_load_agent_overrides_non_object_top_level_degrades(tmp_path, caplog):
    # Previously this passed json.loads and crashed the tick at roster build
    # (AttributeError on list.get). Now it must degrade like malformed JSON.
    p = tmp_path / "agent_overrides.json"
    p.write_text("[1, 2, 3]")
    with caplog.at_level("WARNING"):
        assert load_agent_overrides(p) == {}
    assert "not an object" in caplog.text


def test_load_agent_overrides_non_object_agent_entry_dropped(tmp_path, caplog):
    p = tmp_path / "agent_overrides.json"
    p.write_text('{"femr_v1": "garbage", "twap_mr_v1": {"a": 1}, "basis_v1": null}')
    with caplog.at_level("WARNING"):
        out = load_agent_overrides(p)
    assert out == {"twap_mr_v1": {"a": 1}}, \
        "bad entry dropped, null skipped, good entry kept"
    assert "femr_v1" in caplog.text


def test_build_roster_names_and_validated_defaults(conn):
    agents = build_roster(conn)
    assert [a.name for a in agents] == [
        "femr_v1", "twap_mr_v1", "twap_mr_regime_v1",
        "liq_cascade_v1", "basis_v1", "breakout_v1", "breakout_er_v1",
    ]
    femr = agents[0]
    assert femr.cfg.funding_enter_per_hr == 0.00015
    assert femr.cfg.max_notional_per_trade == 20.0
    breakout = agents[-2]
    assert breakout.cfg.lookback_bars == 384
    assert breakout.cfg.exit_lookback_bars == 96
    assert breakout.cfg.closes_key == "closes_15m"
    assert breakout.cfg.max_total_notional == 60.0
    assert breakout.cfg.min_efficiency_ratio == 0.0, "baseline arm stays unfiltered"
    # B-EDGE2e paper A/B arm: same channel, trend-quality gate ON
    er_arm = agents[-1]
    assert er_arm.cfg.lookback_bars == 384
    assert er_arm.cfg.min_efficiency_ratio == 0.1
    assert er_arm.cfg.er_lookback_bars == 96
    assert er_arm.cfg.closes_key == "closes_15m"
    # the roster's breakout entries must keep driving the 15m feed sizing
    assert closes_15m_bars(agents) == 385


def test_build_roster_overrides_merge_without_bleed(conn):
    agents = build_roster(conn, {
        "femr_v1": {"max_notional_per_trade": 7.0},
        "unknown_agent_v9": {"max_notional_per_trade": 999.0},
    })
    femr = agents[0]
    assert femr.cfg.max_notional_per_trade == 7.0, "override applied"
    assert femr.cfg.funding_enter_per_hr == 0.00015, "untouched default survives"
    others = [a for a in agents if a.name != "femr_v1"]
    assert all(a.config.get("max_notional_per_trade") != 7.0 for a in others), \
        "an override for one agent doesn't bleed into the rest"
