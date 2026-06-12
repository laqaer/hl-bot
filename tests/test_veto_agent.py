"""Tests for the advisory VetoAgent (per-coin account-edge verdicts).

The agent backs the read-only ``hlbot veto`` report (B12j — the vestigial
``hlbot tick`` wrapper that used to run it is retired: since B-PAPER its
logged paper ``place`` rows from the funding_arb skeleton would contaminate
the real paper book). The behavior to pin down:

- the verdict logic: ``veto`` when the lookback edge is below the threshold on
  enough trades, ``allow`` when not, ``no-opinion`` below the trade floor;
- the lookback window actually filters old fills;
- the agent is advisory-only: every decision is a ``hold``, never a place.
"""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.veto import VetoAgent
from hl_bot.db.schema import init_db

NOW_MS = int(time.time() * 1000)


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "veto.sqlite")


def _insert_fill(conn, coin, t_ms, pnl, fee=0.05, sz=10.0, px=1.0):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{coin}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, None, "{}"),
    )


def _seed(conn, coin, n, pnl_each, fee=0.05, age_ms=1000):
    for i in range(n):
        _insert_fill(conn, coin, NOW_MS - age_ms - i, pnl=pnl_each, fee=fee)


def _decide(conn, mids=None, **cfg):
    agent = VetoAgent(config=cfg or None, conn=conn)
    decisions = agent.decide(MarketView(ts_ms=NOW_MS, mids=mids or {}))
    return {d.coin: d for d in decisions}


def test_negative_edge_on_enough_trades_is_vetoed(conn):
    # 25 fills × $10 notional, each net −$1.05 -> edge ≈ −1050 bps << −5 bps.
    _seed(conn, "ZEC", 25, pnl_each=-1.0)
    d = _decide(conn)["ZEC"]
    m = d.market_snapshot
    assert m["verdict"] == "veto"
    assert m["n_trades"] == 25
    assert m["edge_bps"] < -5.0
    assert m["net_pnl"] == pytest.approx(25 * -1.05)


def test_positive_edge_is_allowed(conn):
    _seed(conn, "BTC", 25, pnl_each=+1.0)
    d = _decide(conn)["BTC"]
    assert d.market_snapshot["verdict"] == "allow"
    assert d.market_snapshot["edge_bps"] > 0


def test_below_min_trades_is_no_opinion_even_when_bleeding(conn):
    # Heavy losses but only 5 trades (< default floor 20): small-N noise must
    # not produce a veto.
    _seed(conn, "HYPE", 5, pnl_each=-10.0)
    d = _decide(conn)["HYPE"]
    assert d.market_snapshot["verdict"] == "no-opinion"
    assert d.market_snapshot["n_trades"] == 5


def test_lookback_window_filters_old_fills(conn):
    # 25 losing fills, all OLDER than the lookback: the history is invisible,
    # so a coin still in the view gets no-opinion with zero trades counted.
    _seed(conn, "SOL", 25, pnl_each=-1.0, age_ms=31 * 86_400_000)
    out = _decide(conn, mids={"SOL": 100.0}, lookback_days=30)
    assert out["SOL"].market_snapshot["verdict"] == "no-opinion"
    assert out["SOL"].market_snapshot["n_trades"] == 0
    # With a wider lookback the same fills flip the verdict to veto.
    out = _decide(conn, lookback_days=60)
    assert out["SOL"].market_snapshot["verdict"] == "veto"


def test_threshold_and_floor_are_configurable(conn):
    # Edge ≈ −150 bps on 10 trades: vetoed once both knobs admit the sample.
    _seed(conn, "ETH", 10, pnl_each=-0.1)
    assert _decide(conn, min_trades=10)["ETH"].market_snapshot["verdict"] == "veto"
    assert _decide(conn, min_trades=10, veto_threshold_bps=-200.0)[
        "ETH"].market_snapshot["verdict"] == "allow"


def test_advisory_only_every_action_is_hold(conn):
    _seed(conn, "ZEC", 25, pnl_each=-1.0)
    _seed(conn, "BTC", 25, pnl_each=+1.0)
    agent = VetoAgent(conn=conn)
    decisions = agent.decide(MarketView(ts_ms=NOW_MS, mids={"DOGE": 0.1}))
    assert decisions, "one row per coin with an opinion or a mid"
    assert all(d.action == "hold" for d in decisions)
    assert {d.coin for d in decisions} == {"ZEC", "BTC", "DOGE"}
