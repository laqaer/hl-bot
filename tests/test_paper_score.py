"""Paper-book scorecard (B-PAPER3).

The paper book records forward-test evidence but ``score_agent`` is
fills-based, so paper-only agents score N/A everywhere. ``scoring/paper.py``
replays the is_paper=1 place/flatten rows into synthetic fills under the
backtester's CostModel and aggregates them into the same Scorecard shape —
these tests pin the replay semantics, the cost model, the window filtering,
and the paper/live book separation.
"""

from __future__ import annotations

import pytest

from hl_bot.backtest.engine import CostModel
from hl_bot.db.schema import init_db
from hl_bot.scoring.paper import (
    list_paper_agents,
    mark_paper_positions,
    paper_daily_pnl,
    paper_open_positions,
    replay_paper_fills,
    score_paper_agent,
    score_paper_all,
)

FREE = CostModel(taker_fee_bps=0.0, slippage_bps=0.0)
DAY_MS = 86_400_000


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


# ---------------------------------------------------------------------------
# replay_paper_fills — the pure replay
# ---------------------------------------------------------------------------


def test_round_trip_zero_cost_long():
    fills, open_pos = replay_paper_fills(
        [(1000, "BTC", "place", "B", 2.0, 100.0),
         (2000, "BTC", "flatten", None, None, 110.0)],
        FREE,
    )
    assert open_pos == []
    assert len(fills) == 2
    entry, exit_ = fills
    assert entry.closed_pnl == 0.0 and entry.fee == 0.0 and entry.px == 100.0
    assert exit_.side == "A" and exit_.sz == 2.0
    assert exit_.closed_pnl == pytest.approx(20.0)  # (110-100)*2


def test_round_trip_zero_cost_short():
    fills, _ = replay_paper_fills(
        [(1000, "ETH", "place", "A", 1.0, 200.0),
         (2000, "ETH", "flatten", None, None, 180.0)],
        FREE,
    )
    assert fills[1].side == "B"
    assert fills[1].closed_pnl == pytest.approx(20.0)  # (200-180)*1


def test_taker_costs_match_engine_semantics():
    # 10 bps fee, 20 bps slippage, exaggerated for arithmetic clarity.
    cost = CostModel(taker_fee_bps=10.0, slippage_bps=20.0)
    fills, _ = replay_paper_fills(
        [(1000, "BTC", "place", "B", 1.0, 100.0),
         (2000, "BTC", "flatten", None, None, 100.0)],
        cost,
    )
    entry, exit_ = fills
    assert entry.px == pytest.approx(100.0 * 1.002)   # buy crosses up
    assert exit_.px == pytest.approx(100.0 * 0.998)   # sell crosses down
    assert entry.fee == pytest.approx(entry.px * 0.001)
    assert exit_.fee == pytest.approx(exit_.px * 0.001)
    # Flat price, round trip loses the full spread cross: 99.8 - 100.2.
    assert exit_.closed_pnl == pytest.approx(-0.4)


def test_flatten_without_open_is_skipped():
    fills, open_pos = replay_paper_fills(
        [(1000, "BTC", "flatten", None, None, 100.0)], FREE)
    assert fills == [] and open_pos == []


def test_unfillable_place_rows_are_skipped():
    rows = [
        (1000, "BTC", "place", None, 1.0, 100.0),   # no side
        (1001, "BTC", "place", "B", None, 100.0),   # no size
        (1002, "BTC", "place", "B", 1.0, None),     # no price
    ]
    fills, open_pos = replay_paper_fills(rows, FREE)
    assert fills == [] and open_pos == []


def test_replace_overwrites_like_agent_book():
    # Agents' own replays overwrite on a re-place; the exit closes the LATEST entry.
    fills, open_pos = replay_paper_fills(
        [(1000, "BTC", "place", "B", 1.0, 100.0),
         (2000, "BTC", "place", "B", 1.0, 200.0),
         (3000, "BTC", "flatten", None, None, 210.0)],
        FREE,
    )
    assert open_pos == []
    assert fills[-1].closed_pnl == pytest.approx(10.0)  # vs 200, not 100


def test_open_position_survives_replay():
    fills, open_pos = replay_paper_fills(
        [(1000, "SOL", "place", "B", 3.0, 50.0)], FREE)
    assert len(fills) == 1 and fills[0].closed_pnl == 0.0
    assert len(open_pos) == 1
    pos = open_pos[0]
    assert (pos.coin, pos.side, pos.sz, pos.entry_px) == ("SOL", "B", 3.0, 50.0)


# ---------------------------------------------------------------------------
# score_paper_agent — Scorecard aggregation
# ---------------------------------------------------------------------------


def test_scorecard_shape_and_stats(conn):
    now = 100 * DAY_MS
    # Two closed round trips (one win +20, one loss -5) and one open entry.
    _log(conn, "a", now - 5000, "place", "BTC", "B", 2.0, 100.0)
    _log(conn, "a", now - 4000, "flatten", "BTC", px=110.0)
    _log(conn, "a", now - 3000, "place", "ETH", "A", 1.0, 200.0)
    _log(conn, "a", now - 2000, "flatten", "ETH", px=205.0)
    _log(conn, "a", now - 1000, "place", "SOL", "B", 1.0, 50.0)

    sc = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now)
    assert sc.agent == "a" and sc.window == "all"
    assert sc.n_trades == 5                      # each leg counts, like fills
    assert sc.realized_pnl == pytest.approx(15.0)
    assert sc.fees_paid == 0.0 and sc.funding_pnl == 0.0
    assert sc.net_pnl == pytest.approx(15.0)
    assert sc.win_rate == pytest.approx(0.5)
    assert sc.avg_win == pytest.approx(20.0)
    assert sc.avg_loss == pytest.approx(-5.0)
    assert sc.profit_factor == pytest.approx(4.0)
    # 200+220 (BTC legs) + 200+205 (ETH) + 50 (open SOL entry)
    assert sc.notional_traded == pytest.approx(875.0)
    assert sc.edge_bps == pytest.approx(15.0 / 875.0 * 10_000)


def test_window_filters_fills_not_pairing(conn):
    now = 100 * DAY_MS
    # Entry 2 days ago, exit 2 hours ago: the exit's PnL lands in the 24h
    # window even though the entry leg is outside it (fills-based semantics).
    _log(conn, "a", now - 2 * DAY_MS, "place", "BTC", "B", 1.0, 100.0)
    _log(conn, "a", now - 2 * 3_600_000, "flatten", "BTC", px=120.0)

    sc24 = score_paper_agent(conn, "a", "24h", cost=FREE, now_ms=now)
    assert sc24.n_trades == 1
    assert sc24.realized_pnl == pytest.approx(20.0)
    sc_all = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now)
    assert sc_all.n_trades == 2

    # 1h window has neither leg.
    sc1h = score_paper_agent(conn, "a", "1h", cost=FREE, now_ms=now)
    assert sc1h.n_trades == 0 and sc1h.edge_bps is None


def test_live_rows_invisible_to_paper_score(conn):
    now = 100 * DAY_MS
    _log(conn, "a", now - 2000, "place", "BTC", "B", 1.0, 100.0, paper=False)
    _log(conn, "a", now - 1000, "flatten", "BTC", px=200.0, paper=False)
    sc = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now)
    assert sc.n_trades == 0 and sc.net_pnl == 0.0


def test_sharpe_and_drawdown_need_days_and_capital(conn):
    now = 100 * DAY_MS
    # Three days of round trips: +10, -5, +10.
    for i, (entry, exit_) in enumerate([(100.0, 110.0), (100.0, 95.0), (100.0, 110.0)]):
        t = now - (3 - i) * DAY_MS
        _log(conn, "a", t, "place", "BTC", "B", 1.0, entry)
        _log(conn, "a", t + 1000, "flatten", "BTC", px=exit_)
    sc = score_paper_agent(conn, "a", "all", cost=FREE, now_ms=now)
    assert sc.sharpe is not None and sc.sharpe > 0
    assert sc.max_drawdown is None              # no capital base given
    sc_cap = score_paper_agent(conn, "a", "all", cost=FREE, capital_base=100.0, now_ms=now)
    assert sc_cap.max_drawdown == pytest.approx(-0.05 / 1.10, rel=1e-6)


def test_paper_open_positions_and_roster(conn):
    now = 100 * DAY_MS
    _log(conn, "a", now - 1000, "place", "BTC", "B", 1.0, 100.0)
    _log(conn, "b", now - 1000, "place", "ETH", "A", 1.0, 200.0)
    _log(conn, "b", now - 500, "flatten", "ETH", px=190.0)
    _log(conn, "c", now - 100, "hold", None)                       # not executable
    _log(conn, "d", now - 100, "place", "SOL", "B", 1.0, 50.0, paper=False)

    assert list_paper_agents(conn) == ["a", "b"]
    assert [p.coin for p in paper_open_positions(conn, "a", FREE)] == ["BTC"]
    assert paper_open_positions(conn, "b", FREE) == []

    cards = score_paper_all(conn)
    assert {c.agent for c in cards} == {"a", "b"}
    assert {c.window for c in cards} == {"24h", "7d", "30d", "all"}


def test_paper_daily_pnl_gap_filled(conn):
    """Daily series zero-fills idle days, matching the live track-record
    series, and modeled funding lands on its own day."""
    now = 100 * DAY_MS
    # Round trips on day 96 (+10) and day 98 (-5); day 97 idle.
    for day, (entry, exit_) in [(96, (100.0, 110.0)), (98, (100.0, 95.0))]:
        _log(conn, "a", day * DAY_MS + 1000, "place", "BTC", "B", 1.0, entry)
        _log(conn, "a", day * DAY_MS + 2000, "flatten", "BTC", px=exit_)
    daily = paper_daily_pnl(conn, "a", cost=FREE, now_ms=now)
    assert daily == pytest.approx([10.0, 0.0, -5.0])

    # A funding event mid-hold lands on its own day (here: the exit day).
    _log(conn, "b", int(96.5 * DAY_MS), "place", "ETH", "B", 1.0, 100.0)
    _log(conn, "b", int(97.5 * DAY_MS), "flatten", "ETH", px=100.0)
    rates = {"ETH": [{"time": 97 * DAY_MS + 1000, "fundingRate": "0.0001"}]}
    daily_b = paper_daily_pnl(conn, "b", cost=FREE, funding_by_coin=rates, now_ms=now)
    assert daily_b == pytest.approx([0.0, -0.01])    # day 96 entry, day 97 funding

    assert paper_daily_pnl(conn, "nobody", cost=FREE, now_ms=now) == []


# ---------------------------------------------------------------------------
# mark_paper_positions — open positions marked to market (B-PAPER3d)
# ---------------------------------------------------------------------------


def test_mark_long_and_short_zero_cost():
    _, open_long = replay_paper_fills([(1000, "BTC", "place", "B", 2.0, 100.0)], FREE)
    _, open_short = replay_paper_fills([(1000, "ETH", "place", "A", 1.0, 200.0)], FREE)
    [ml] = mark_paper_positions(open_long, {"BTC": 110.0}, FREE)
    [ms] = mark_paper_positions(open_short, {"ETH": 180.0}, FREE)
    assert (ml.mark_px, ml.upnl) == (110.0, pytest.approx(20.0))   # (110-100)*2
    assert (ms.mark_px, ms.upnl) == (180.0, pytest.approx(20.0))   # (200-180)*1


def test_mark_matches_flatten_semantics():
    """The no-double-count invariant: marking an open position at px must
    equal the closed_pnl − fee a real flatten row at px would realize, so
    card-realized + open-uPnL is the flattened-right-now book value."""
    cost = CostModel(taker_fee_bps=10.0, slippage_bps=20.0)
    for side, mark in (("B", 110.0), ("A", 90.0)):
        entry_rows = [(1000, "BTC", "place", side, 2.0, 100.0)]
        _, open_pos = replay_paper_fills(entry_rows, cost)
        [m] = mark_paper_positions(open_pos, {"BTC": mark}, cost)
        fills, left = replay_paper_fills(
            entry_rows + [(2000, "BTC", "flatten", None, None, mark)], cost)
        assert left == []
        exit_ = fills[-1]
        assert m.upnl == pytest.approx(exit_.closed_pnl - exit_.fee)


def test_mark_missing_or_bad_mid_is_unmarked():
    _, open_pos = replay_paper_fills(
        [(1000, "SOL", "place", "B", 3.0, 50.0),
         (1001, "DOGE", "place", "B", 10.0, 0.1)], FREE)
    marked = mark_paper_positions(open_pos, {"DOGE": 0.0})   # SOL absent, DOGE ≤ 0
    assert [m.coin for m in marked] == [p.coin for p in open_pos]
    assert all(m.mark_px is None and m.upnl is None for m in marked)
    # Entry fields survive untouched for display.
    assert marked[0].entry_px == 50.0 and marked[0].sz == 3.0


def test_cli_score_paper_marks_open_positions(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from hl_bot.cli import main as cli

    c = init_db(":memory:")
    _log(c, "breakout_v1", 1000, "place", "SOL", "B", 2.0, 100.0)
    monkeypatch.setattr(cli, "_conn", lambda: (c, None))
    monkeypatch.setattr(cli, "console", Console(width=250))

    monkeypatch.setattr(cli, "_fetch_mids", lambda s: {"SOL": 110.0})
    res = CliRunner().invoke(cli.app, ["score", "--paper", "--no-funding"])
    assert res.exit_code == 0, res.output
    assert "marked at current mid" in res.output
    assert "Open paper uPnL" in res.output and "breakout_v1 +19." in res.output

    # --no-mark (and a failed fetch, which degrades to {}) stays unmarked.
    res2 = CliRunner().invoke(
        cli.app, ["score", "--paper", "--no-funding", "--no-mark"])
    assert res2.exit_code == 0, res2.output
    assert "not marked to market" in res2.output
    assert "Open paper uPnL" not in res2.output
    c.close()


def test_default_cost_is_taker(conn):
    # Flat round trip at default costs must lose fees + slippage, comparable
    # to the backtester's taker arm.
    now = 100 * DAY_MS
    _log(conn, "a", now - 2000, "place", "BTC", "B", 1.0, 100.0)
    _log(conn, "a", now - 1000, "flatten", "BTC", px=100.0)
    sc = score_paper_agent(conn, "a", "all", now_ms=now)
    assert sc.net_pnl < 0
    # ~2*(4.5bps fee) + 2*(2bps slip) on ~$100 notional ≈ $0.13
    assert sc.net_pnl == pytest.approx(-0.13, abs=0.01)
    assert sc.edge_bps == pytest.approx(-6.5, abs=0.1)
