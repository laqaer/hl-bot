"""enrich_view breadth + concurrency (P2 — accelerate the forward flywheel).

The forward soak's breadth was capped at a hardcoded top-20 fetched serially.
Widening it (more coins -> more dislocations/funding-fade events -> faster G0)
is the lever, but only if it stays inside the cycle budget. These pin: the
universe size is honoured, the bounded-concurrency path is deterministic vs
serial, and a single bad coin never drops the rest of the universe.
"""

from __future__ import annotations

import hl_bot.engine.views as views
from hl_bot.agents.base import MarketView


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.Client: deterministic candleSnapshot per coin, empty
    spot/liquidation feeds. Thread-safe (stateless), so it exercises the pool."""

    fail_coins: set[str] = set()

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        t = (json or {}).get("type")
        if t == "candleSnapshot":
            coin = json["req"]["coin"]
            if coin in self.fail_coins:
                raise RuntimeError(f"boom {coin}")
            # 60 synthetic bars; price level keyed off the coin so coins differ
            base = 100.0 + (hash(coin) % 50)
            return _Resp([{"c": str(base + i * 0.1), "v": "10"} for i in range(60)])
        if t == "spotMetaAndAssetCtxs":
            return _Resp([{"universe": [], "tokens": []}, []])
        if t == "liquidations":
            return _Resp([])
        return _Resp({})


def _vol(n: int) -> dict[str, float]:
    # higher index -> higher volume, so the top-N are COIN{n-1}..COIN{n-N}
    return {f"COIN{i}": float(i) for i in range(n)}


def _enrich(monkeypatch, vol, *, fail=(), **kw):
    fc = type("FC", (_FakeClient,), {"fail_coins": set(fail)})
    monkeypatch.setattr(views.httpx, "Client", fc)
    view = MarketView(ts_ms=0, mids={}, funding={})
    views.enrich_view(view, "http://x", vol, **kw)
    return view


def test_universe_size_caps_and_picks_top_volume(monkeypatch):
    view = _enrich(monkeypatch, _vol(50), universe_size=10, max_workers=4)
    got = set(view.extra["candles_5m"])
    assert len(got) == 10
    # the top-10 by volume are COIN49..COIN40
    assert got == {f"COIN{i}" for i in range(40, 50)}
    assert set(view.extra["candles_1h"]) == got  # both signals computed


def test_parallel_matches_serial(monkeypatch):
    vol = _vol(25)
    par = _enrich(monkeypatch, vol, universe_size=20, max_workers=8)
    ser = _enrich(monkeypatch, vol, universe_size=20, max_workers=1)
    assert par.extra["candles_5m"] == ser.extra["candles_5m"]
    assert par.extra["candles_1h"] == ser.extra["candles_1h"]
    assert len(par.extra["candles_5m"]) == 20


def test_per_coin_failure_is_isolated(monkeypatch):
    view = _enrich(monkeypatch, _vol(12), universe_size=12, max_workers=4,
                   fail={"COIN5"})
    assert "COIN5" not in view.extra["candles_5m"]
    assert len(view.extra["candles_5m"]) == 11   # the other 11 survive


def test_size_zero_disables_candles(monkeypatch):
    view = _enrich(monkeypatch, _vol(30), universe_size=0, max_workers=4)
    assert view.extra["candles_5m"] == {} and view.extra["candles_1h"] == {}


# --- staggered (round-robin) refresh -----------------------------------------

class _CountingClient(_FakeClient):
    """Records which coins were actually fetched (candleSnapshot) this call."""
    fetched: list[str] = []

    def post(self, url, json=None):
        if (json or {}).get("type") == "candleSnapshot":
            type(self).fetched.append(json["req"]["coin"])
        return super().post(url, json)


def _enrich_counting(monkeypatch, vol, *, carry=None, **kw):
    cc = type("CC", (_CountingClient,), {"fetched": [], "fail_coins": set()})
    monkeypatch.setattr(views.httpx, "Client", cc)
    view = MarketView(ts_ms=0, mids={}, funding={}, extra=dict(carry or {}))
    views.enrich_view(view, "http://x", vol, carry_extra=carry, **kw)
    return view, cc.fetched


def test_refresh_limit_fetches_only_a_round_robin_window(monkeypatch):
    vol = _vol(10)  # top-10 = COIN9..COIN0 (desc by vol)
    top = [f"COIN{i}" for i in range(9, -1, -1)]
    _, fetched = _enrich_counting(monkeypatch, vol, universe_size=10,
                                  max_workers=4, refresh_limit=4, rotate_offset=0)
    # only 4 coins fetched (each = 2 candleSnapshot calls), the first window
    assert {c for c in fetched} == set(top[:4])
    assert len(fetched) == 8  # 4 coins x (1m + 5m)


def test_rotation_carries_forward_unrefreshed_and_covers_all(monkeypatch):
    vol = _vol(8)
    top = [f"COIN{i}" for i in range(7, -1, -1)]
    extra: dict = {}
    seen: set[str] = set()
    # 2 cycles of refresh_limit=4 over a universe of 8 -> full coverage, and the
    # carried-forward coins keep their stats so candles_5m always spans all 8.
    offset = 0
    sizes = []
    for _ in range(2):
        view, fetched = _enrich_counting(monkeypatch, vol, carry=extra,
                                         universe_size=8, max_workers=4,
                                         refresh_limit=4, rotate_offset=offset)
        seen |= {c for c in fetched}
        sizes.append(len(view.extra["candles_5m"]))
        extra = dict(view.extra)
        offset = (offset + 4) % 8
    assert sizes == [4, 8]   # cold start: 4 fresh; then carry 4 + fresh 4 = whole universe
    assert seen == set(top)  # every coin refreshed across the two cycles


def test_refresh_limit_zero_fetches_whole_universe(monkeypatch):
    _, fetched = _enrich_counting(monkeypatch, _vol(6), universe_size=6,
                                  max_workers=3, refresh_limit=0)
    assert len({c for c in fetched}) == 6
