"""Evidence test for dislocation_reversion_v1.

The thesis: a violent, volatility-normalized move that overshoots its short-
window VWAP partially reverts within minutes. The agent fades the extreme
(z<=-z_enter -> LONG, z>=+z_enter -> SHORT) with a maker-resting entry, a tight
stop, and a short max-hold. These tests pin entry direction, the entry gate,
each exit reason (take-profit / stop / max-hold), and that a fade-and-revert
round trip is net positive through the engine with real maker costs.

The agent reads vwap/sigma exactly like twap_mr_regime:
``view.extra['candles_5m'][coin] = {'vwap': float, 'sigma': float}``, which the
backtest engine plumbs from ``Frame.candles_5m``.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.dislocation_reversion import DislocationReversionAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame, frozen_clock
from hl_bot.db.schema import init_db

MIN = 60_000
COIN = "TST"
VOL = 50_000_000.0


def _view(mid: float, vwap: float, sigma: float, ts_ms: int = 0) -> MarketView:
    return MarketView(
        ts_ms=ts_ms,
        mids={COIN: mid},
        funding={COIN: 0.0},
        extra={
            "day_ntl_vlm": {COIN: VOL},
            # the agent reads candles_5m (5m/5h); the engine aliases Frame.candles_1h
            # to it for backtests, but the direct-decide helper builds the view by hand.
            "candles_5m": {COIN: {"vwap": vwap, "sigma": sigma, "n": 60}},
            "closes": {COIN: [vwap, mid]},
        },
    )


def _agent(config: dict | None = None):
    conn = init_db(":memory:")
    agent = DislocationReversionAgent(config=config or {}, conn=conn)
    return agent, conn


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------


def test_fade_down_dislocation_goes_long():
    agent, _ = _agent()
    # mid 4 sigma BELOW vwap -> z=-4 -> fade LONG (B).
    decisions = agent.decide(_view(mid=96.0, vwap=100.0, sigma=1.0))
    places = [d for d in decisions if d.action == "place"]
    assert len(places) == 1
    assert places[0].coin == COIN
    assert places[0].side == "B"


def test_fade_up_dislocation_goes_short():
    agent, _ = _agent()
    # mid 4 sigma ABOVE vwap -> z=+4 -> fade SHORT (A).
    decisions = agent.decide(_view(mid=104.0, vwap=100.0, sigma=1.0))
    places = [d for d in decisions if d.action == "place"]
    assert len(places) == 1
    assert places[0].side == "A"


def test_no_fade_when_z_below_enter():
    agent, _ = _agent()
    # |z|=2 < z_enter(3) -> hold only, no place.
    decisions = agent.decide(_view(mid=98.0, vwap=100.0, sigma=1.0))
    assert all(d.action != "place" for d in decisions)
    assert any(d.action == "hold" for d in decisions)


# --------------------------------------------------------------------------
# exits — seed an open position via the agent's own audit log, then decide.
# --------------------------------------------------------------------------


def _seed_long(agent, conn, *, entry_px: float, sz: float = 0.25, ts_ms: int = 0):
    """Record a paper 'place' (long) at a controlled ts_ms so _open_positions()
    replays it (log_decision stamps its own clock, so write the row directly)."""
    conn.execute(
        """INSERT INTO agent_decisions(
               ts_ms, agent, action, coin, side, sz, px, cloid,
               reasoning, market_snapshot, is_paper, error
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, agent.name, "place", COIN, "B", sz, entry_px, "seed",
         None, "{}", 1, None),
    )


def test_take_profit_exit_on_reversion():
    agent, conn = _agent()
    _seed_long(agent, conn, entry_px=96.0)
    # price reverted back to vwap: z = (100-100)/1 = 0, within z_exit(0.5).
    with frozen_clock(MIN / 1000.0):
        decisions = agent.decide(_view(mid=100.0, vwap=100.0, sigma=1.0, ts_ms=MIN))
    flats = [d for d in decisions if d.action == "flatten"]
    assert len(flats) == 1
    assert "REVERTED" in (flats[0].reasoning or "")


def test_stop_exit_when_price_drops_further():
    agent, conn = _agent(config={"stop_pct": 0.015})
    _seed_long(agent, conn, entry_px=96.0)
    # long entered at 96; price drops 2% to ~94.08 -> adverse > stop_pct -> STOP.
    mid = 96.0 * (1 - 0.02)
    with frozen_clock(MIN / 1000.0):
        decisions = agent.decide(_view(mid=mid, vwap=100.0, sigma=1.0, ts_ms=MIN))
    flats = [d for d in decisions if d.action == "flatten"]
    assert len(flats) == 1
    assert "STOP" in (flats[0].reasoning or "")


def test_max_hold_exit():
    # Position that neither reverts (still |z|>z_exit) nor stops (no adverse move)
    # must exit after max_hold_bars. Keep z negative so CROSSED doesn't trigger.
    agent, conn = _agent(config={"max_hold_bars": 12, "bar_seconds": 300})
    _seed_long(agent, conn, entry_px=96.0, ts_ms=0)
    # mid still well below vwap (z=-3, not reverted, not crossed, not adverse vs
    # entry since mid>=entry). 13 bars * 300s elapsed -> MAX-HOLD.
    elapsed_s = 13 * 300
    with frozen_clock(elapsed_s):
        decisions = agent.decide(_view(mid=97.0, vwap=100.0, sigma=1.0,
                                        ts_ms=elapsed_s * 1000))
    flats = [d for d in decisions if d.action == "flatten"]
    assert len(flats) == 1
    assert "MAX-HOLD" in (flats[0].reasoning or "")


# --------------------------------------------------------------------------
# end-to-end: fade-and-revert round trip is net positive with maker costs
# --------------------------------------------------------------------------


def test_reversion_profit_through_engine():
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = DislocationReversionAgent(config={}, conn=conn)

    frames = [
        # bar 0: sharp drop, mid 4 sigma below vwap -> agent fades LONG at 96.
        Frame(ts_ms=0, mids={COIN: 96.0}, funding={COIN: 0.0},
              day_ntl_vlm={COIN: VOL},
              candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
              closes={COIN: [100.0, 96.0]}),
        # bar 1: partial recovery, still dislocated (z=-2) -> hold the long.
        Frame(ts_ms=1 * MIN, mids={COIN: 98.0}, funding={COIN: 0.0},
              day_ntl_vlm={COIN: VOL},
              candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
              closes={COIN: [100.0, 98.0]}),
        # bar 2: full reversion to vwap (z=0) -> take-profit flatten at 100.
        Frame(ts_ms=2 * MIN, mids={COIN: 100.0}, funding={COIN: 0.0},
              day_ntl_vlm={COIN: VOL},
              candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
              closes={COIN: [100.0, 100.0]}),
    ]
    res = bt.run(agent, frames)
    # Bought the wick at 96, sold the reversion at 100 (~+4% gross) — clears
    # maker entry + taker exit costs.
    assert res.net_pnl > 0
    assert res.scorecard.n_fills >= 2
