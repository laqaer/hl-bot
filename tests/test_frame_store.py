"""Forward frame store (P1 linchpin): rebuild confirm frames from the per-bar
signal the engine accrues each cycle, so a retention-capped 5m agent's G0 window
GROWS forward past HL's ~17.5d instead of just rolling.

Pins: accrual writes per-bar rows (floored, idempotent), the loader reconstructs
agent-compatible Frames (a dislocation actually trades on them), and the merge
unions HL's candles with accrued bars so the window extends backward.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.dislocation_reversion import DislocationReversionAgent
from hl_bot.backtest.data import load_accrued_frames, merge_frames
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db
from hl_bot.ingest.accrual import accrue_frame_samples

MIN5 = 300_000


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def _view(now_ms, coin, mid, vwap, sigma, *, vol=5e7, funding=1e-5):
    return MarketView(
        ts_ms=now_ms, mids={coin: mid}, funding={coin: funding},
        extra={"day_ntl_vlm": {coin: vol}, "funding_hourly": {coin: funding},
               "candles_5m": {coin: {"vwap": vwap, "sigma": sigma, "n": 60}}},
    )


def test_migration_creates_frame_samples(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(frame_samples)").fetchall()}
    assert {"interval", "coin", "bar_ts_ms", "mid", "funding_hourly",
            "vwap", "sigma", "vol"} <= cols


def _accrue(conn, now_ms, coin, mid, vwap, sigma, **kw):
    return accrue_frame_samples(conn, _view(now_ms, coin, mid, vwap, sigma, **kw),
                                now_ms=now_ms)


def test_accrual_floors_to_bar_and_is_idempotent(conn):
    base = 100 * MIN5
    # two cycles inside the same 5m bar -> one row (first wins)
    assert _accrue(conn, base + 10_000, "BTC", 100.0, 99.0, 1.0) == 1
    assert _accrue(conn, base + 200_000, "BTC", 103.0, 99.0, 1.0) == 0
    row = conn.execute("SELECT bar_ts_ms, mid, vwap FROM frame_samples "
                       "WHERE interval='5m' AND coin='BTC'").fetchone()
    assert row["bar_ts_ms"] == base and row["mid"] == 100.0  # first observation kept
    # next bar -> new row
    assert _accrue(conn, base + MIN5, "BTC", 105.0, 99.0, 1.0) == 1


def test_loader_reconstructs_agent_compatible_frames(conn):
    # Accrue a dislocation: mid sits at vwap, then gaps to z=+5, then reverts.
    coin = "BTC"
    seq = [(99.0, 99.0, 1.0), (99.0, 99.0, 1.0), (104.0, 99.0, 1.0),  # z=+5 -> SHORT
           (99.0, 99.0, 1.0)]                                          # revert -> exit
    for i, (mid, vwap, sigma) in enumerate(seq):
        _accrue(conn, 100 * MIN5 + i * MIN5, coin, mid, vwap, sigma)
    frames = load_accrued_frames(conn, [coin], "5m")
    assert len(frames) == 4
    f = frames[2]
    assert f.mids[coin] == 104.0
    assert f.candles_1h[coin] == {"vwap": 99.0, "sigma": 1.0, "n": 60}  # aliased to candles_5m
    assert f.funding_hourly[coin] == 1e-5
    assert f.funding[coin] == pytest.approx(1e-5 / 12)  # per-bar = hourly * (5m/1h)
    assert f.closes[coin][-1] == 104.0 and len(f.closes[coin]) == 3   # trailing window

    # The reconstructed frames actually DRIVE the agent: dislocation fades the
    # z=+5 overshoot (SHORT) and exits on revert — proving frame-compatibility.
    bt = Backtester(CostModel(maker=False), conn=init_db(":memory:"))
    res = bt.run(DislocationReversionAgent(config={}, conn=bt.conn), frames)
    assert res.scorecard.n_trades >= 1


def test_merge_unions_and_extends_window():
    coin = "BTC"
    accrued = [Frame(ts_ms=t * MIN5, mids={coin: 100.0 + t}) for t in range(0, 6)]
    # HL only "retains" the last 3 bars (3,4,5), with its own official mids.
    back = [Frame(ts_ms=t * MIN5, mids={coin: 999.0}) for t in (3, 4, 5)]
    merged = merge_frames(back, accrued)
    ts = [f.ts_ms for f in merged]
    assert ts == sorted(ts) and len(merged) == 6           # window extended back to bar 0
    # HL's official frame wins on overlap; accrued fills the bars HL forgot.
    by_ts = {f.ts_ms: f for f in merged}
    assert by_ts[5 * MIN5].mids[coin] == 999.0             # back-fetched wins
    assert by_ts[0].mids[coin] == 100.0                    # accrued-only bar survives


def test_loader_filters_by_coin_and_since(conn):
    _accrue(conn, 100 * MIN5, "BTC", 100.0, 99.0, 1.0)
    _accrue(conn, 101 * MIN5, "ETH", 50.0, 49.0, 1.0)
    assert {c for f in load_accrued_frames(conn, ["BTC"], "5m") for c in f.mids} == {"BTC"}
    later = load_accrued_frames(conn, [], "5m", since_ms=101 * MIN5)
    assert len(later) == 1 and later[0].ts_ms == 101 * MIN5
