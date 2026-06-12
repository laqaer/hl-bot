"""Confirmation-harness tests.

The harness must (a) confirm a genuinely profitable mean-reversion strategy in a
range-bound market under maker execution, and (b) refuse a strategy that only
loses (fading a trend). If it can't tell those apart it's worthless.
"""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.confirm import confirm_strategy
from hl_bot.backtest.engine import Frame

HOUR = 3_600_000
COIN = "TST"


def _choppy(n: int = 40) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 103.0 if i % 2 else 100.0      # oscillate around flat VWAP
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


def _uptrend(n: int = 40) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 100.0 + 1.0 * i
        closes.append(mid)
        frames.append(Frame(
            ts_ms=i * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": mid - 4.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


def test_confirms_profitable_mean_reversion_as_maker():
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _choppy(), prefer="maker", min_sharpe=0.5, min_trades=2,
    )
    assert res.confirmed
    assert res.in_sample.edge_bps and res.in_sample.edge_bps > 0
    assert res.out_of_sample.edge_bps and res.out_of_sample.edge_bps > 0


def test_thin_sample_not_confirmed_even_with_positive_edge():
    """A positive edge on a handful of trades is noise, not a G0 pass.

    Same profitable fixture as above, judged at the default trade floor: the
    splits hold far fewer than 20 trades each, so the verdict must be FAIL
    with an explicit too-thin reason (a real 1d carry run 'passed' on 2
    in-sample trades before this floor existed).
    """
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _choppy(), prefer="maker", min_sharpe=0.5,
    )
    assert res.in_sample.edge_bps and res.in_sample.edge_bps > 0
    assert not res.confirmed
    assert any("too thin" in r for r in res.reasons)


def test_rejects_trend_fader():
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _uptrend(), prefer="taker",
    )
    assert not res.confirmed
    assert res.reasons


def test_insufficient_data_not_confirmed():
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _choppy(4),
    )
    assert not res.confirmed
