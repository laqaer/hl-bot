"""Confirmation-harness tests.

The harness must (a) confirm a genuinely profitable mean-reversion strategy in a
range-bound market under maker execution, and (b) refuse a strategy that only
loses (fading a trend). If it can't tell those apart it's worthless.
"""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.confirm import confirm_strategy, max_window_pnl_share
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


# ---------------------------------------------------------------------------
# Profit time-concentration (pocket) diagnostic
# ---------------------------------------------------------------------------


def test_pocket_share_diffuse_edge_earns_window_frac():
    # Equity rises 1/hour for 100 hours: any 25h window earns 25 of 100.
    curve = [(i * HOUR, 1_000.0 + i) for i in range(101)]
    got = max_window_pnl_share(curve)
    assert got is not None
    share, _, _ = got
    assert abs(share - 0.25) < 0.02


def test_pocket_share_one_pocket_is_near_one_and_dated():
    # Flat except a 10-hour burst in the middle: the burst IS the whole net.
    curve, v = [], 1_000.0
    for i in range(101):
        if 45 <= i < 55:
            v += 10.0
        curve.append((i * HOUR, v))
    got = max_window_pnl_share(curve)
    assert got is not None
    share, t0, t1 = got
    assert share >= 0.99
    # The reported window brackets the burst, not the flat tails.
    assert 40 * HOUR <= t0 <= 50 * HOUR
    assert 50 * HOUR <= t1 <= 60 * HOUR


def test_pocket_share_exceeds_one_when_rest_of_sample_loses():
    # +200 in the pocket, -1/hour everywhere else: net +110, pocket gain ~200.
    curve, v = [(0, 1_000.0)], 1_000.0
    for i in range(1, 101):
        v += 20.0 if 40 <= i < 50 else -1.0
        curve.append((i * HOUR, v))
    got = max_window_pnl_share(curve)
    assert got is not None
    assert got[0] > 1.0


def test_pocket_share_none_for_losing_or_thin_curves():
    losing = [(i * HOUR, 1_000.0 - i) for i in range(50)]
    assert max_window_pnl_share(losing) is None
    assert max_window_pnl_share([(0, 1_000.0), (HOUR, 1_001.0)]) is None


def test_confirm_reports_pocket_on_profitable_scenarios():
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _choppy(), prefer="maker", min_sharpe=0.5, min_trades=2,
    )
    assert res.confirmed  # unchanged by the diagnostic
    assert res.in_sample.pocket_share is not None
    assert res.in_sample.pocket_window and ".." in res.in_sample.pocket_window
    assert res.in_sample.pocket_window_frac == 0.25
    assert "pocket" in res.summary()


def test_losing_confirm_has_no_pocket_noise():
    res = confirm_strategy(
        lambda conn: TwapMrAgent(config={}, conn=conn),
        _uptrend(), prefer="taker",
    )
    assert res.in_sample.pocket_share is None
    assert "pocket" not in res.summary()
