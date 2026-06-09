"""Confirmation tests for the time-series (absolute) momentum strategy.

TS-momentum trades each coin independently on the sign of its *own* trailing
return — net directional, not a dollar-neutral rank. We check:
  * it LONGs an up-trend and SHORTs a down-trend beyond the entry band;
  * unlike the cross-sectional book it takes NET exposure (two up-trends → two
    longs, no forced short);
  * a calm name inside the band is never traded;
  * the ``reversion`` flag fades the trend (long the faller, short the riser);
  * too-short history holds;
  * it books a positive PnL when a trend continues, maker.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.ts_momentum import TsMomentumAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000


def _view(closes: dict[str, list[float]], vol: float = 5e7) -> MarketView:
    mids = {c: s[-1] for c, s in closes.items()}
    return MarketView(
        ts_ms=0, mids=mids,
        extra={"closes": closes, "day_ntl_vlm": {c: vol for c in closes}},
    )


def _ramp(start: float, pct: float, n: int = 30) -> list[float]:
    end = start * (1 + pct)
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def test_longs_uptrend_shorts_downtrend():
    closes = {
        "UP": _ramp(100.0, 0.10),
        "DOWN": _ramp(100.0, -0.10),
        "FLAT": _ramp(100.0, 0.0),
    }
    agent = TsMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["UP"].side == "B"
    assert decs["DOWN"].side == "A"
    assert "FLAT" not in decs


def test_takes_net_directional_exposure():
    # Two up-trends: a *time-series* book longs BOTH (net long) — there is no
    # cross-sectional constraint forcing a short of the weaker one.
    closes = {"UP1": _ramp(100.0, 0.12), "UP2": _ramp(100.0, 0.06)}
    agent = TsMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    sides = {d.coin: d.side for d in agent.decide(_view(closes)) if d.action == "place"}
    assert sides == {"UP1": "B", "UP2": "B"}


def test_subthreshold_trend_not_traded():
    closes = {"A": _ramp(100.0, 0.005), "B": _ramp(100.0, -0.005)}  # ±0.5% < 2% entry
    agent = TsMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    assert {d.action for d in agent.decide(_view(closes))} == {"hold"}


def test_reversion_flag_fades_the_trend():
    closes = {"UP": _ramp(100.0, 0.10), "DOWN": _ramp(100.0, -0.10)}
    agent = TsMomentumAgent(
        config={"lookback_bars": 24, "reversion": True}, conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["UP"].side == "A"    # fade the riser
    assert decs["DOWN"].side == "B"  # buy the faller


def test_short_series_holds():
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 49.0, 48.0]}
    agent = TsMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_books_positive_pnl_on_a_continuing_trend():
    conn = init_db(":memory:")
    frames = []
    for i in range(40):
        f = 1 + i * 0.004
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"UP": 100.0 * f, "DOWN": 100.0 / f, "FLAT": 50.0},
            day_ntl_vlm={"UP": 5e7, "DOWN": 5e7, "FLAT": 5e7},
            closes={
                "UP": [100.0 * (1 + j * 0.004) for j in range(max(0, i - 29), i + 1)],
                "DOWN": [100.0 / (1 + j * 0.004) for j in range(max(0, i - 29), i + 1)],
                "FLAT": [50.0] * min(30, i + 1),
            },
        ))
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = TsMomentumAgent(config={"lookback_bars": 24}, conn=conn)
    res = bt.run(agent, frames)
    traded = {r[0] for r in conn.execute("SELECT DISTINCT coin FROM fills").fetchall()}
    assert "FLAT" not in traded
    assert {"UP", "DOWN"} <= traded
    assert res.net_pnl > 0
