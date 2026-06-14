"""Funding history fetch: forward pagination past the 500-row cap, and 429
retry/backoff. Both are load-bearing for funding-as-signal agents — without
pagination the recent funding is missing; without backoff a multi-coin sweep
gets rate-limited and silently reports 0 trades (a false negative)."""

from __future__ import annotations

import hl_bot.backtest.data as d

H = 3_600_000
BASE = 1_700_000_000_000


def test_funding_window_pages_forward(monkeypatch):
    # Simulate HL's 500-row cap anchored at startTime over 1200 hourly rows.
    full = [{"time": BASE + i * H, "fundingRate": 0.0001} for i in range(1200)]

    def fake(coin, start, end, base_url="x"):
        return [r for r in full if start <= r["time"] <= end][:500]

    monkeypatch.setattr(d, "fetch_funding_history", fake)
    out = d.fetch_funding_history_window("BTC", BASE, BASE + 1200 * H)
    times = [r["time"] for r in out]
    assert len(out) == 1200                 # all pages collected
    assert times == sorted(times)           # ordered
    assert len(set(times)) == 1200          # de-duplicated across page seams


def test_funding_window_handles_single_short_page(monkeypatch):
    full = [{"time": BASE + i * H, "fundingRate": 0.0} for i in range(50)]
    monkeypatch.setattr(
        d, "fetch_funding_history",
        lambda coin, start, end, base_url="x": [r for r in full if start <= r["time"] <= end])
    out = d.fetch_funding_history_window("BTC", BASE, BASE + 50 * H)
    assert len(out) == 50


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Context-manager httpx.Client stand-in popping queued responses."""
    def __init__(self, queue):
        self._q = queue

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        return self._q.pop(0)


def test_post_info_retries_on_429_then_succeeds(monkeypatch):
    queue = [_Resp(429, None), _Resp(429, None), _Resp(200, {"ok": 1})]
    monkeypatch.setattr(d.httpx, "Client", lambda timeout=20.0: _Client(queue))
    monkeypatch.setattr(d.time, "sleep", lambda s: None)   # no real backoff in tests
    out = d._post_info({"type": "x"}, base_url="http://t", retries=5)
    assert out == {"ok": 1}
    assert queue == []                      # consumed both 429s and the 200


def test_post_info_surfaces_429_after_exhausting_retries(monkeypatch):
    queue = [_Resp(429, None) for _ in range(3)]
    monkeypatch.setattr(d.httpx, "Client", lambda timeout=20.0: _Client(queue))
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    try:
        d._post_info({"type": "x"}, base_url="http://t", retries=2)
        raise AssertionError("expected the 429 to surface")
    except RuntimeError as e:
        assert "429" in str(e)
