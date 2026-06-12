"""Rolling candle store tests (B-HIST) — fake fetch, tmp dirs, no network.

These pin down what makes the harvester trustworthy as an unattended cron job:
  1. Merge semantics: dedup by open time, fresh wins (a bar fetched while
     still forming is finalized by the next harvest), ascending order.
  2. Incremental fetch starts at the last stored bar (inclusive), so nothing
     is skipped and nothing already-final is refetched wholesale.
  3. An empty store reaches back one full retention window — everything HL
     still has.
  4. One bad pair records an error and the sweep continues; good pairs are
     saved regardless.
"""

from __future__ import annotations

import pytest

from hl_bot.backtest.data import CANDLE_PAGE_LIMIT
from hl_bot.backtest.store import (
    harvest,
    harvest_one,
    load_store,
    merge_candles,
    save_store,
    store_path,
)

MIN = 60_000


def _bar(t: int, c: float = 100.0, v: float = 1.0) -> dict:
    return {"t": t, "T": t + MIN - 1, "o": c, "h": c, "l": c, "c": c, "v": v, "n": 1}


# ---------------------------------------------------------------------------
# merge_candles
# ---------------------------------------------------------------------------


def test_merge_dedups_sorts_and_fresh_wins():
    existing = [_bar(2 * MIN, v=1.0), _bar(0)]          # unsorted on purpose
    fresh = [_bar(2 * MIN, v=42.0), _bar(3 * MIN)]      # overlaps the last bar
    merged = merge_candles(existing, fresh)
    assert [r["t"] for r in merged] == [0, 2 * MIN, 3 * MIN]
    assert merged[1]["v"] == 42.0  # refetched (final) version replaced the partial one


def test_merge_drops_rows_without_valid_t():
    merged = merge_candles([{"c": 1.0}, {"t": "junk"}], [_bar(MIN), {"t": None}])
    assert [r["t"] for r in merged] == [MIN]


# ---------------------------------------------------------------------------
# store round trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    path = store_path("BTC", "1m", tmp_path)
    assert path.name == "BTC_1m.json.gz"
    assert load_store(path) == []  # missing file is an empty store
    bars = [_bar(0), _bar(MIN)]
    save_store(path, bars)
    assert load_store(path) == bars


# ---------------------------------------------------------------------------
# harvest_one
# ---------------------------------------------------------------------------


def test_first_harvest_reaches_back_one_retention_window(tmp_path):
    now = 1_000_000 * MIN
    calls: list[tuple] = []

    def fake_fetch(coin, interval, start, end, *, base_url):
        calls.append((coin, interval, start, end))
        return [_bar(t) for t in range(end - 3 * MIN, end, MIN)]

    res = harvest_one("BTC", "1m", root=tmp_path, now_ms=now, fetch=fake_fetch)
    assert calls == [("BTC", "1m", now - CANDLE_PAGE_LIMIT * MIN, now)]
    assert res.error is None
    assert (res.added, res.total) == (3, 3)
    assert res.span_days == pytest.approx(2 * MIN / 86_400_000)
    assert len(load_store(store_path("BTC", "1m", tmp_path))) == 3


def test_incremental_harvest_refetches_last_bar_and_appends(tmp_path):
    path = store_path("BTC", "1m", tmp_path)
    save_store(path, [_bar(0), _bar(MIN, v=1.0)])  # last bar captured mid-formation
    calls: list[tuple] = []

    def fake_fetch(coin, interval, start, end, *, base_url):
        calls.append(start)
        return [_bar(MIN, v=42.0), _bar(2 * MIN)]  # finalized old bar + one new

    res = harvest_one("BTC", "1m", root=tmp_path, now_ms=3 * MIN, fetch=fake_fetch)
    assert calls == [MIN]  # starts at the last stored open time, inclusive
    assert (res.added, res.total) == (1, 3)  # the refetched bar isn't "added"
    stored = load_store(path)
    assert [r["t"] for r in stored] == [0, MIN, 2 * MIN]
    assert stored[1]["v"] == 42.0


def test_harvest_sweep_isolates_failures(tmp_path):
    def fake_fetch(coin, interval, start, end, *, base_url):
        if coin == "BAD":
            raise RuntimeError("boom")
        return [_bar(0)]

    results = harvest(["BAD", "GOOD"], ("1m",), root=tmp_path, now_ms=MIN, fetch=fake_fetch)
    bad, good = results
    assert bad.error == "boom" and bad.total == 0
    assert not store_path("BAD", "1m", tmp_path).exists()
    assert good.error is None and good.total == 1
    assert load_store(store_path("GOOD", "1m", tmp_path)) == [_bar(0)]


def test_noop_harvest_adds_nothing(tmp_path):
    path = store_path("BTC", "1m", tmp_path)
    save_store(path, [_bar(0)])

    res = harvest_one("BTC", "1m", root=tmp_path, now_ms=MIN,
                      fetch=lambda *a, **k: [_bar(0)])
    assert (res.added, res.total) == (0, 1)
    assert load_store(path) == [_bar(0)]
