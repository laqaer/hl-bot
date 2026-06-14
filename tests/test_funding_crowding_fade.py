"""Evidence test for funding_crowding_fade_v1 (D2a).

The thesis: when funding is well above the ~11% baseline (crowded) AND price has
overshot its 5m VWAP in that direction, fading the overshoot reverts. The agent
fires only when BOTH hold, with the overshoot SIGN ALIGNED to funding, and bounds
the tail with a hard stop. These tests pin entry direction, all three gates
(funding / z / sign-alignment), each exit reason, and a net-positive round trip
through the engine — which also exercises the new ``funding_hourly`` plumbing
(unscaled 1h rate, identical units in backtest and live).
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.funding_crowding_fade import FundingCrowdingFadeAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame, frozen_clock
from hl_bot.db.schema import init_db

BAR = 300_000  # 5m
COIN = "TST"
VOL = 50_000_000.0
# funding_hourly for a target APR: rate = apr% / (100 * 24 * 365).
APR_HIGH = 0.00005   # ≈ +43.8% APR (>> 20% default gate)
APR_LOW = 0.000001   # ≈ +0.88% APR (<< gate)


def _view(mid, vwap, sigma, funding_hourly, ts_ms=0):
    return MarketView(
        ts_ms=ts_ms,
        mids={COIN: mid},
        funding={COIN: 0.0},
        extra={
            "day_ntl_vlm": {COIN: VOL},
            "candles_5m": {COIN: {"vwap": vwap, "sigma": sigma, "n": 60}},
            "funding_hourly": {COIN: funding_hourly},
        },
    )


def _agent(config=None):
    conn = init_db(":memory:")
    return FundingCrowdingFadeAgent(config=config or {}, conn=conn), conn


def _places(decisions):
    return [d for d in decisions if d.action == "place"]


# --------------------------------------------------------------------------
# entries — direction & gates
# --------------------------------------------------------------------------

def test_crowded_long_overshoot_goes_short():
    # +funding (crowded long) + mid 2σ ABOVE vwap (z=+2) -> SHORT.
    agent, _ = _agent()
    p = _places(agent.decide(_view(102.0, 100.0, 1.0, APR_HIGH)))
    assert len(p) == 1 and p[0].coin == COIN and p[0].side == "A"


def test_crowded_short_overshoot_goes_long():
    # -funding (crowded short) + mid 2σ BELOW vwap (z=-2) -> LONG.
    agent, _ = _agent()
    p = _places(agent.decide(_view(98.0, 100.0, 1.0, -APR_HIGH)))
    assert len(p) == 1 and p[0].side == "B"


def test_no_entry_when_funding_below_gate():
    # Big overshoot but funding at baseline -> no trade (this is what separates
    # it from dislocation: the funding gate is mandatory).
    agent, _ = _agent()
    assert not _places(agent.decide(_view(102.0, 100.0, 1.0, APR_LOW)))


def test_no_entry_when_z_below_enter():
    # High funding but z below z_enter (1.0): mid only 0.5σ above vwap.
    agent, _ = _agent()
    assert not _places(agent.decide(_view(100.5, 100.0, 1.0, APR_HIGH)))


def test_no_entry_when_sign_misaligned():
    # +funding (crowded long) but price gapped DOWN (z<0): not a crowded
    # overshoot to fade -> no trade.
    agent, _ = _agent()
    assert not _places(agent.decide(_view(98.0, 100.0, 1.0, APR_HIGH)))


# --------------------------------------------------------------------------
# exits
# --------------------------------------------------------------------------

def _seed_short(agent, conn, *, entry_px, ts_ms=0, sz=0.25):
    conn.execute(
        """INSERT INTO agent_decisions(
               ts_ms, agent, action, coin, side, sz, px, cloid,
               reasoning, market_snapshot, is_paper, error
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, agent.name, "place", COIN, "A", sz, entry_px, "seed", "seed", "{}", 1, None),
    )


def test_exit_stop_on_adverse():
    agent, conn = _agent()
    _seed_short(agent, conn, entry_px=100.0, ts_ms=0)
    # short, price up 3% -> adverse 3% >= stop 2% -> STOP.
    with frozen_clock(BAR / 1000.0):
        d = agent.decide(_view(103.0, 101.0, 1.0, APR_HIGH, ts_ms=BAR))
    flats = [x for x in d if x.action == "flatten"]
    assert len(flats) == 1 and "STOP" in flats[0].reasoning


def test_exit_reverted_at_z_exit():
    agent, conn = _agent()
    _seed_short(agent, conn, entry_px=102.0, ts_ms=0)
    # z back to ~0 (mid==vwap) -> |z|<=z_exit -> REVERTED (take profit).
    with frozen_clock(BAR / 1000.0):
        d = agent.decide(_view(100.0, 100.0, 1.0, APR_HIGH, ts_ms=BAR))
    flats = [x for x in d if x.action == "flatten"]
    assert len(flats) == 1 and "REVERTED" in flats[0].reasoning


def test_exit_max_hold():
    agent, conn = _agent()
    _seed_short(agent, conn, entry_px=100.3, ts_ms=0)
    # z still elevated (1.5>z_exit, >0 so not crossed) and no stop, but held
    # beyond max_hold_bars (12 bars) -> MAX-HOLD.
    held = 13 * BAR
    with frozen_clock(held / 1000.0):
        d = agent.decide(_view(100.3, 100.0, 0.2, APR_HIGH, ts_ms=held))
    flats = [x for x in d if x.action == "flatten"]
    assert len(flats) == 1 and "MAX-HOLD" in flats[0].reasoning


# --------------------------------------------------------------------------
# engine round trip (also pins funding_hourly plumbing through Frame)
# --------------------------------------------------------------------------

def _frame(ts, mid, vwap, sigma, fh):
    return Frame(
        ts_ms=ts, mids={COIN: mid}, funding={COIN: 0.0},
        day_ntl_vlm={COIN: VOL},
        candles_1h={COIN: {"vwap": vwap, "sigma": sigma, "n": 60}},
        funding_hourly={COIN: fh},
    )


def test_engine_round_trip_net_positive():
    # crowded-long overshoot (z=+2.5, +funding) -> short -> reverts to vwap.
    frames = [
        _frame(0 * BAR, 100.0, 100.0, 1.0, APR_HIGH),       # z=0, no entry
        _frame(1 * BAR, 102.5, 100.0, 1.0, APR_HIGH),       # z=+2.5 -> SHORT
        _frame(2 * BAR, 100.0, 100.0, 1.0, APR_HIGH),       # z=0 -> REVERTED exit
        _frame(3 * BAR, 100.0, 100.0, 1.0, APR_HIGH),
    ]
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = FundingCrowdingFadeAgent(config={}, conn=conn)
    res = bt.run(agent, frames)
    assert res.scorecard.n_trades >= 1
    # entered short ~102.5, exited ~100 -> a clean ~2.5% reversion beats costs.
    assert res.net_pnl > 0
