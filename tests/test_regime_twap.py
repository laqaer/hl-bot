"""Evidence test for twap_mr_regime_v1.

The whole point of the regime filter is to stop the "fade a breakout, lose,
fade again" loss loop. So the test that matters is: on a strong trend, the
baseline TWAP shorts the strength and bleeds, while the regime-filtered variant
refuses those fades and does not. In a choppy market both should behave the same.
"""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.agents.twap_mr_regime import TwapMrRegimeAgent
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000
COIN = "TST"


def _uptrend_frames(n: int = 25) -> list[Frame]:
    """Monotonic uptrend with VWAP lagging below -> persistent z>2 short signal."""
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 100.0 + 1.0 * i          # steady rip up
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={COIN: mid},
            funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": mid - 4.0, "sigma": 1.0, "n": 60}},  # z=+4
            closes={COIN: list(closes)},
        ))
    return frames


def _run(agent_cls, frames):
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True, maker_fee_bps=0.0), conn=conn)  # cost-free: isolate direction
    return bt.run(agent_cls(config={}, conn=conn), frames)


def test_regime_filter_avoids_fading_the_trend():
    frames = _uptrend_frames()
    baseline = _run(TwapMrAgent, frames)
    regime = _run(TwapMrRegimeAgent, frames)

    # Baseline shorts the uptrend and loses; regime refuses and doesn't.
    assert baseline.net_pnl < 0
    assert regime.net_pnl > baseline.net_pnl
    assert regime.scorecard.n_trades < baseline.scorecard.n_trades


def test_regime_allows_fades_in_choppy_market():
    # Oscillation around a flat VWAP: mean reversion works, filter must NOT block.
    frames = []
    closes: list[float] = []
    for i in range(8):
        mid = 103.0 if i % 2 else 100.0
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={COIN: mid},
            funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    regime = _run(TwapMrRegimeAgent, frames)
    # It still trades (fades the spikes) since the market is range-bound.
    assert regime.scorecard.n_trades > 0
