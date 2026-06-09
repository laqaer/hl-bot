"""Multi-window robustness-harness tests.

Iteration 20's lesson, pinned down: a strategy that clears G0 on one window but
reverses sign on a disjoint window is *not* durable. ``confirm_across_windows``
must (a) call that out, (b) require >=2 windows (one window is the trailing-only
trap), and (c) bless an edge only when it survives every disjoint window.
"""

from __future__ import annotations

from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.backtest.confirm import confirm_across_windows
from hl_bot.backtest.engine import Frame

HOUR = 3_600_000
COIN = "TST"


def _choppy(n: int = 40, ts0: int = 0) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 103.0 if i % 2 else 100.0      # oscillate around flat VWAP
        closes.append(mid)
        frames.append(Frame(
            ts_ms=(ts0 + i) * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": 100.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


def _uptrend(n: int = 40, ts0: int = 0) -> list[Frame]:
    frames = []
    closes: list[float] = []
    for i in range(n):
        mid = 100.0 + 1.0 * i
        closes.append(mid)
        frames.append(Frame(
            ts_ms=(ts0 + i) * HOUR, mids={COIN: mid}, funding={COIN: 0.0},
            day_ntl_vlm={COIN: 50_000_000.0},
            candles_1h={COIN: {"vwap": mid - 4.0, "sigma": 1.0, "n": 60}},
            closes={COIN: list(closes)},
        ))
    return frames


def _factory(conn):
    return TwapMrAgent(config={}, conn=conn)


def test_durable_when_edge_survives_every_window():
    res = confirm_across_windows(
        _factory,
        [("choppy-A", _choppy(ts0=0)), ("choppy-B", _choppy(ts0=200))],
        prefer="maker", min_sharpe=0.5,
    )
    assert res.durable
    assert len(res.windows) == 2
    assert all(w.confirmation.confirmed for w in res.windows)


def test_not_durable_when_sign_flips_across_windows():
    # mean-reversion confirms on the choppy window but loses on the trend window;
    # the full-sample edge flips sign -> the artifact signature, not a durable edge.
    res = confirm_across_windows(
        _factory,
        [("choppy", _choppy(ts0=0)), ("uptrend", _uptrend(ts0=200))],
        prefer="maker", min_sharpe=0.5,
    )
    assert not res.durable
    assert any("FLIPS SIGN" in r for r in res.reasons)
    assert any("uptrend" in r for r in res.reasons)


def test_single_window_is_never_durable():
    # one window is exactly the Iteration-20 trap; durability requires >=2.
    res = confirm_across_windows(
        _factory, [("trailing-only", _choppy())], prefer="maker", min_sharpe=0.5,
    )
    assert not res.durable
    assert any("need >=2" in r for r in res.reasons)
