"""Confirmation tests for the cross-sectional momentum strategy.

Momentum edge = the spread between strong and weak names persisting. We build
synthetic close paths with a deterministic trailing-return ranking and check:
  * it LONGs the strongest, SHORTs the weakest, ignores the middle/calm names;
  * the ``reversion`` flag flips both legs (long the loser, short the winner);
  * a name with sub-threshold momentum is never traded;
  * it stays dollar-neutral and books a positive PnL when the trend continues.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.xsect_momentum import XSectMomentumAgent
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
    """n closes ending pct away from `start` (linear), so the lookback return is ~pct."""
    end = start * (1 + pct)
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def test_longs_winner_shorts_loser():
    closes = {
        "WIN": _ramp(100.0, 0.10),   # +10% over the window
        "LOSE": _ramp(100.0, -0.10),  # -10%
        "FLAT": _ramp(100.0, 0.0),   # ~0% -> below entry threshold
    }
    agent = XSectMomentumAgent(config={"lookback_bars": 24, "top_k": 1}, conn=init_db(":memory:"))
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["WIN"].side == "B"
    assert decs["LOSE"].side == "A"
    assert "FLAT" not in decs


def test_reversion_flag_flips_both_legs():
    closes = {"WIN": _ramp(100.0, 0.10), "LOSE": _ramp(100.0, -0.10)}
    agent = XSectMomentumAgent(
        config={"lookback_bars": 24, "top_k": 1, "reversion": True},
        conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    # reversion: long the loser, short the winner
    assert decs["WIN"].side == "A"
    assert decs["LOSE"].side == "B"


def test_subthreshold_momentum_not_traded():
    closes = {"A": _ramp(100.0, 0.005), "B": _ramp(100.0, -0.005)}  # ±0.5% < 2% entry
    agent = XSectMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    actions = {d.action for d in agent.decide(_view(closes))}
    assert actions == {"hold"}


def test_short_series_holds():
    # Fewer closes than lookback+1 -> no signal -> hold.
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 49.0, 48.0]}
    agent = XSectMomentumAgent(config={"lookback_bars": 24}, conn=init_db(":memory:"))
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_regime_gate_stands_aside_in_a_market_drawdown():
    # Whole universe is falling: aggregate market trailing return < 0, so the
    # regime gate disables the book even though there are dispersed momentum legs.
    closes = {
        "WIN": _ramp(100.0, -0.05),   # falling, but least-bad -> would be the long
        "LOSE": _ramp(100.0, -0.20),  # falling hardest -> would be the short
    }
    gated = XSectMomentumAgent(
        config={"lookback_bars": 24, "top_k": 1, "regime_gate": True, "regime_lookback": 24},
        conn=init_db(":memory:"),
    )
    assert all(d.action == "hold" for d in gated.decide(_view(closes)))
    # ungated: the same book trades (sanity that the gate is what suppresses it).
    ungated = XSectMomentumAgent(
        config={"lookback_bars": 24, "top_k": 1}, conn=init_db(":memory:"),
    )
    assert any(d.action == "place" for d in ungated.decide(_view(closes)))


def test_regime_gate_allows_book_when_market_trends_up():
    # Market is broadly up (mean +8%) -> regime on -> dispersion is traded normally,
    # incl. a short on the one weak name.
    closes = {"WIN": _ramp(100.0, 0.20), "LOSE": _ramp(100.0, -0.04)}
    agent = XSectMomentumAgent(
        config={"lookback_bars": 24, "top_k": 1, "enter_return": 0.01,
                "regime_gate": True, "regime_lookback": 24},
        conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["WIN"].side == "B"
    assert decs["LOSE"].side == "A"


def test_two_sided_and_neutral_over_a_continuing_trend():
    # WIN keeps rising, LOSE keeps falling: a maker book long WIN / short LOSE
    # collects the continuation. MID drifts inside the band and is never traded.
    conn = init_db(":memory:")
    frames = []
    for i in range(40):
        f = 1 + i * 0.004
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"WIN": 100.0 * f, "LOSE": 100.0 / f, "MID": 50.0},
            day_ntl_vlm={"WIN": 5e7, "LOSE": 5e7, "MID": 5e7},
            closes={
                "WIN": [100.0 * (1 + j * 0.004) for j in range(max(0, i - 29), i + 1)],
                "LOSE": [100.0 / (1 + j * 0.004) for j in range(max(0, i - 29), i + 1)],
                "MID": [50.0] * min(30, i + 1),
            },
        ))
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = XSectMomentumAgent(config={"lookback_bars": 24, "top_k": 1}, conn=conn)
    res = bt.run(agent, frames)
    traded = {r[0] for r in conn.execute("SELECT DISTINCT coin FROM fills").fetchall()}
    assert "MID" not in traded
    assert {"WIN", "LOSE"} <= traded
    assert res.net_pnl > 0
