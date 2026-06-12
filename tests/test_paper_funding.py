"""Modeled funding accrual over paper holds (B-PAPER3a).

Paper scorecards reported funding_pnl=0, so a funding strategy's (femr's)
paper book was unjudgeable — its revenue line was invisible. These tests pin
the hold replay (same book semantics as ``replay_paper_fills``), the per-event
accrual arithmetic (the engine's ``-signed × notional × rate``, marked at the
entry mid), the event-time window/daily semantics (mirroring how live
``funding_payments`` are scored), and the fetch-span helper.
"""

from __future__ import annotations

import pytest

from hl_bot.backtest.engine import CostModel
from hl_bot.db.schema import init_db
from hl_bot.scoring.paper import (
    PaperHold,
    modeled_funding_events,
    paper_funding_spans,
    replay_paper_holds,
    score_paper_agent,
)

FREE = CostModel(taker_fee_bps=0.0, slippage_bps=0.0)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _log(conn, agent, ts_ms, action, coin, side=None, sz=None, px=None, paper=True):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(?,?,?,?,?,?,?,?)""",
        (ts_ms, agent, action, coin, side, sz, px, 1 if paper else 0),
    )


def _frow(t, rate):
    # HL fundingHistory returns the rate as a string.
    return {"coin": "X", "fundingRate": str(rate), "premium": "0.0", "time": t}


# ---------------------------------------------------------------------------
# replay_paper_holds — hold spans under the book's replay rules
# ---------------------------------------------------------------------------


def test_holds_round_trip_and_open():
    holds = replay_paper_holds([
        (1000, "BTC", "place", "B", 2.0, 100.0),
        (2000, "BTC", "flatten", None, None, 110.0),
        (3000, "SOL", "place", "A", 1.0, 50.0),
    ])
    assert len(holds) == 2
    btc, sol = holds
    assert (btc.coin, btc.side, btc.sz) == ("BTC", "B", 2.0)
    assert btc.entry_px == 100.0           # raw logged mid, no slippage
    assert (btc.entry_ts_ms, btc.exit_ts_ms) == (1000, 2000)
    assert sol.exit_ts_ms is None          # still open


def test_replace_drops_the_old_hold():
    # Same semantics as replay_paper_fills: the overwritten hold never produces
    # an exit fill, so it accrues no funding either.
    holds = replay_paper_holds([
        (1000, "BTC", "place", "B", 1.0, 100.0),
        (2000, "BTC", "place", "B", 1.0, 200.0),
        (3000, "BTC", "flatten", None, None, 210.0),
    ])
    assert len(holds) == 1
    assert (holds[0].entry_ts_ms, holds[0].exit_ts_ms, holds[0].entry_px) == (2000, 3000, 200.0)


def test_unfillable_place_and_orphan_flatten_open_nothing():
    holds = replay_paper_holds([
        (1000, "BTC", "place", None, 1.0, 100.0),    # no side
        (1001, "BTC", "place", "B", None, 100.0),    # no size
        (1002, "BTC", "place", "B", 1.0, None),      # no price
        (1003, "ETH", "flatten", None, None, 100.0),  # nothing open
    ])
    assert holds == []


def test_flatten_with_missing_px_still_ends_the_hold():
    # The book closes on every flatten (replay_paper_fills pops before its px
    # check); the hold span ends there even though no exit fill is produced.
    holds = replay_paper_holds([
        (1000, "BTC", "place", "B", 1.0, 100.0),
        (2000, "BTC", "flatten", None, None, None),
    ])
    assert len(holds) == 1 and holds[0].exit_ts_ms == 2000


# ---------------------------------------------------------------------------
# modeled_funding_events — the engine's accrual, event-at-its-own-timestamp
# ---------------------------------------------------------------------------


def test_long_pays_short_receives_positive_rate():
    rows = {"X": [_frow(HOUR_MS, 0.0001)]}
    long_ = PaperHold("X", "B", 2.0, 100.0, 0, 10 * HOUR_MS)
    short = PaperHold("X", "A", 2.0, 100.0, 0, 10 * HOUR_MS)
    assert modeled_funding_events([long_], rows, now_ms=0) == [
        (HOUR_MS, pytest.approx(-0.02))]   # -(+2) * 100 * 1e-4
    assert modeled_funding_events([short], rows, now_ms=0) == [
        (HOUR_MS, pytest.approx(+0.02))]


def test_event_boundaries_exclusive_entry_inclusive_exit():
    rows = {"X": [_frow(t, 0.0001) for t in (1000, 2000, 3000, 4000)]}
    hold = PaperHold("X", "A", 1.0, 100.0, 1000, 3000)
    events = modeled_funding_events([hold], rows, now_ms=0)
    # t=1000 is at entry (excluded), 2000/3000 inside, 4000 after exit.
    assert [t for t, _ in events] == [2000, 3000]


def test_open_hold_accrues_up_to_now():
    rows = {"X": [_frow(t, 0.0001) for t in (1000, 2000, 3000)]}
    hold = PaperHold("X", "A", 1.0, 100.0, 0, None)
    events = modeled_funding_events([hold], rows, now_ms=2000)
    assert [t for t, _ in events] == [1000, 2000]


def test_zero_rate_and_unknown_coin_emit_nothing():
    rows = {"X": [_frow(1000, 0.0)]}
    holds = [PaperHold("X", "B", 1.0, 100.0, 0, 2000),
             PaperHold("Y", "B", 1.0, 100.0, 0, 2000)]
    assert modeled_funding_events(holds, rows, now_ms=0) == []


# ---------------------------------------------------------------------------
# score_paper_agent — funding threaded into net / windows / daily Sharpe
# ---------------------------------------------------------------------------


def test_score_includes_modeled_funding(conn):
    now = 100 * DAY_MS
    # femr-shaped: short at flat price, collecting one positive funding event.
    _log(conn, "a", now - 3 * HOUR_MS, "place", "BTC", "A", 1.0, 100.0)
    _log(conn, "a", now - 1 * HOUR_MS, "flatten", "BTC", px=100.0)
    rates = {"BTC": [_frow(now - 2 * HOUR_MS, 0.0001)]}
    sc = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now, funding_by_coin=rates)
    assert sc.realized_pnl == pytest.approx(0.0)
    assert sc.funding_pnl == pytest.approx(0.01)   # short collects 100 * 1e-4
    assert sc.net_pnl == pytest.approx(0.01)
    assert sc.edge_bps == pytest.approx(0.01 / 200.0 * 10_000)
    # Without rates the same book scores funding=0 (offline behavior).
    sc0 = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now)
    assert sc0.funding_pnl == 0.0 and sc0.net_pnl == 0.0


def test_window_filters_funding_by_event_time(conn):
    now = 100 * DAY_MS
    # Hold spans 3 days; one funding event 2 days ago, one 2 hours ago.
    _log(conn, "a", now - 3 * DAY_MS, "place", "BTC", "A", 1.0, 100.0)
    _log(conn, "a", now - 1 * HOUR_MS, "flatten", "BTC", px=100.0)
    rates = {"BTC": [_frow(now - 2 * DAY_MS, 0.0001),
                     _frow(now - 2 * HOUR_MS, 0.0001)]}
    sc24 = score_paper_agent(conn, "a", "24h", cost=FREE, now_ms=now, funding_by_coin=rates)
    assert sc24.funding_pnl == pytest.approx(0.01)   # only the recent event
    sc_all = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now, funding_by_coin=rates)
    assert sc_all.funding_pnl == pytest.approx(0.02)


def test_funding_only_daily_series_feeds_sharpe(conn):
    now = 100 * DAY_MS
    # Open short collecting varying funding across 3 distinct days: the daily
    # series exists even with no closes, so Sharpe evaluates (like live
    # funding_payments do for a carry agent).
    _log(conn, "a", now - 4 * DAY_MS, "place", "BTC", "A", 1.0, 100.0)
    rates = {"BTC": [_frow(now - 3 * DAY_MS, 0.0001),
                     _frow(now - 2 * DAY_MS, 0.0002),
                     _frow(now - 1 * DAY_MS, 0.0001)]}
    sc = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now, funding_by_coin=rates)
    assert sc.funding_pnl == pytest.approx(0.04)
    assert sc.sharpe is not None and sc.sharpe > 0


def test_paper_funding_spans_cover_all_agents_and_open_holds(conn):
    now = 100 * DAY_MS
    _log(conn, "a", 1000, "place", "BTC", "B", 1.0, 100.0)
    _log(conn, "a", 5000, "flatten", "BTC", px=100.0)
    _log(conn, "b", 3000, "place", "BTC", "A", 1.0, 100.0)   # still open → now
    _log(conn, "b", 2000, "place", "ETH", "A", 1.0, 100.0)
    _log(conn, "b", 4000, "flatten", "ETH", px=100.0)
    _log(conn, "c", 9000, "place", "SOL", "B", 1.0, 100.0, paper=False)  # live: ignored
    spans = paper_funding_spans(conn, now_ms=now)
    assert spans == {"BTC": (1000, now), "ETH": (2000, 4000)}
