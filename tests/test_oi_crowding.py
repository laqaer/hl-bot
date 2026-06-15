"""S8 — OI-spike crowding reversal (a forward-confirmable edge).

OI is not in candle history, so this edge can only be confirmed FORWARD: the
per-bar OI-change is accrued into frame_samples and replayed by confirm. These
pin the whole path — the live OI-change signal, the frame-store round trip, the
agent's fade logic, and an end-to-end backtest on reconstructed frames.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.oi_crowding_reversal import OICrowdingReversalAgent
from hl_bot.backtest.data import load_accrued_frames
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db
from hl_bot.ingest.accrual import accrue_frame_samples, build_oi_change_view

MIN5 = 300_000


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def _view(now_ms, coin, mid, *, vwap, sigma, oi=None, oi_change=None, vol=5e7):
    extra = {"day_ntl_vlm": {coin: vol},
             "candles_5m": {coin: {"vwap": vwap, "sigma": sigma, "n": 60}}}
    if oi_change is not None:
        extra["oi_change"] = {coin: oi_change}
    return MarketView(ts_ms=now_ms, mids={coin: mid}, funding={coin: 0.0},
                      open_interest={coin: oi} if oi is not None else {}, extra=extra)


# --- migration + signal ------------------------------------------------------

def test_migration_adds_oi_change_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(frame_samples)").fetchall()}
    assert "oi_change" in cols


def test_build_oi_change_from_accrued_oi(conn):
    now = 1000 * MIN5
    # a reference OI 30min ago, OI now is +20%
    conn.execute("INSERT INTO market_samples(ts_ms, coin, open_interest) VALUES(?,?,?)",
                 (now - 1800_000, "BTC", 1000.0))
    view = _view(now, "BTC", 100.0, vwap=99.0, sigma=1.0, oi=1200.0)
    out = build_oi_change_view(conn, view, now_ms=now, lookback_s=1800)
    assert out["BTC"] == pytest.approx(0.20)
    assert view.extra["oi_change"]["BTC"] == pytest.approx(0.20)


def test_oi_change_absent_without_reference(conn):
    now = 1000 * MIN5
    view = _view(now, "ETH", 50.0, vwap=49.0, sigma=1.0, oi=500.0)
    out = build_oi_change_view(conn, view, now_ms=now, lookback_s=1800)
    assert "ETH" not in out   # no prior OI -> no signal (warmup)


def test_frame_store_round_trips_oi_change(conn):
    now = 1000 * MIN5
    v = _view(now, "BTC", 104.0, vwap=99.0, sigma=1.0, oi_change=0.15)
    accrue_frame_samples(conn, v, now_ms=now)
    frames = load_accrued_frames(conn, ["BTC"], "5m")
    assert len(frames) == 1
    assert frames[0].oi_change["BTC"] == pytest.approx(0.15)


# --- agent logic -------------------------------------------------------------

def _decide(conn, coin, mid, *, vwap, sigma, oi_change):
    agent = OICrowdingReversalAgent(config={"z_enter": 1.0, "oi_spike_min": 0.10},
                                    conn=conn)
    return agent.decide(_view(0, coin, mid, vwap=vwap, sigma=sigma, oi_change=oi_change))


def test_fades_up_overshoot_when_oi_spikes(conn):
    # mid 3 sigma above vwap (z=+3) AND OI +15% -> SHORT
    out = _decide(conn, "BTC", 103.0, vwap=100.0, sigma=1.0, oi_change=0.15)
    place = [d for d in out if d.action == "place"]
    assert len(place) == 1 and place[0].side == "A"  # short the overshoot


def test_fades_down_overshoot_when_oi_spikes(conn):
    out = _decide(conn, "BTC", 97.0, vwap=100.0, sigma=1.0, oi_change=0.15)
    place = [d for d in out if d.action == "place"]
    assert len(place) == 1 and place[0].side == "B"  # long the dip


def test_holds_without_oi_spike(conn):
    # same overshoot, but OI flat -> no crowding gate -> hold
    out = _decide(conn, "BTC", 103.0, vwap=100.0, sigma=1.0, oi_change=0.02)
    assert all(d.action == "hold" for d in out)


def test_holds_without_overshoot(conn):
    # OI spiked but price at vwap (z~0) -> nothing to fade
    out = _decide(conn, "BTC", 100.0, vwap=100.0, sigma=1.0, oi_change=0.20)
    assert all(d.action == "hold" for d in out)


# --- end-to-end backtest on reconstructed frames -----------------------------

def test_backtest_trades_on_oi_crowding_frames():
    coin = "BTC"
    # flat at vwap, then a crowded +5 sigma overshoot, then revert.
    seq = [(100.0, 0.0), (100.0, 0.0), (105.0, 0.15), (100.0, 0.0), (100.0, 0.0)]
    frames = [Frame(ts_ms=i * MIN5, mids={coin: mid},
                    candles_1h={coin: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
                    day_ntl_vlm={coin: 5e7}, oi_change={coin: oic})
              for i, (mid, oic) in enumerate(seq)]
    bt = Backtester(CostModel(maker=False), conn=init_db(":memory:"))
    res = bt.run(OICrowdingReversalAgent(config={"z_enter": 2.0, "oi_spike_min": 0.10},
                                         conn=bt.conn), frames)
    assert res.scorecard.n_trades >= 1   # entered the crowded overshoot and exited
