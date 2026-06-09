"""Confirmation tests for the cross-sectional return-skewness / MAX strategy.

The lottery-demand (MAX) thesis = SHORT the most positively-skewed / highest-MAX
names / LONG the least. We build synthetic close paths with a deterministic
skew/MAX ranking and check:
  * it SHORTs the highest-MAX coins and LONGs the lowest-MAX coins;
  * the ``invert`` flag flips both legs;
  * it needs at least ``2*top_k`` eligible coins to form both legs, else holds;
  * thin (sub-volume) coins are filtered out;
  * a too-short series yields a hold;
  * the ``max`` signal equals the mean of the top-``n_max`` log-returns;
  * the ``skew`` signal is signed correctly (right-skew positive, symmetric ~0);
  * an unknown ``signal`` config is rejected.
"""

from __future__ import annotations

import math

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.xsect_skew import XSectSkewAgent
from hl_bot.db.schema import init_db


def _view(closes: dict[str, list[float]], vol: dict[str, float] | float = 5e7) -> MarketView:
    mids = {c: s[-1] for c, s in closes.items()}
    vmap = vol if isinstance(vol, dict) else {c: vol for c in closes}
    return MarketView(
        ts_ms=0, mids=mids,
        extra={"closes": closes, "day_ntl_vlm": vmap},
    )


def _from_rets(rets: list[float], start: float = 100.0) -> list[float]:
    out = [start]
    for r in rets:
        out.append(out[-1] * math.exp(r))
    return out


def _spiked(base: float, spike: float, n: int = 24) -> list[float]:
    """Small symmetric oscillation of size ``base`` with one big positive ``spike``
    near the end -> MAX (largest return) is dominated by ``spike``."""
    rets = [(base if i % 2 else -base) for i in range(n)]
    rets[-3] = spike
    return _from_rets(rets)


def test_shorts_highest_max_longs_lowest_max():
    closes = {
        "DULL": _spiked(0.001, 0.02),
        "MILD": _spiked(0.001, 0.05),
        "HOT": _spiked(0.001, 0.10),
        "LOTTO": _spiked(0.001, 0.20),
    }
    agent = XSectSkewAgent(
        config={"skew_lookback": 20, "n_max": 1, "top_k": 1}, conn=init_db(":memory:")
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["LOTTO"].side == "A"   # highest MAX -> short
    assert decs["DULL"].side == "B"    # lowest MAX -> long
    assert "MILD" not in decs and "HOT" not in decs


def test_invert_flag_flips_both_legs():
    closes = {
        "DULL": _spiked(0.001, 0.02),
        "LOTTO": _spiked(0.001, 0.20),
    }
    agent = XSectSkewAgent(
        config={"skew_lookback": 20, "n_max": 1, "top_k": 1, "invert": True},
        conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["LOTTO"].side == "B"   # invert: long the lottery
    assert decs["DULL"].side == "A"


def test_needs_enough_coins_for_both_legs():
    closes = {
        "A": _spiked(0.001, 0.02),
        "B": _spiked(0.001, 0.05),
        "C": _spiked(0.001, 0.10),
    }
    agent = XSectSkewAgent(config={"skew_lookback": 20, "top_k": 2}, conn=init_db(":memory:"))
    actions = {d.action for d in agent.decide(_view(closes))}
    assert actions == {"hold"}


def test_thin_coins_filtered_out():
    closes = {
        "DULL": _spiked(0.001, 0.02),
        "LOTTO": _spiked(0.001, 0.20),
        "THIN": _spiked(0.001, 0.50),  # would be the biggest MAX, but no volume
    }
    vol = {"DULL": 5e7, "LOTTO": 5e7, "THIN": 1.0}
    agent = XSectSkewAgent(
        config={"skew_lookback": 20, "n_max": 1, "top_k": 1}, conn=init_db(":memory:")
    )
    decs = {d.coin: d for d in agent.decide(_view(closes, vol)) if d.action == "place"}
    assert "THIN" not in decs
    assert decs["LOTTO"].side == "A"   # LOTTO is the short despite THIN being hotter


def test_short_series_holds():
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 49.0, 48.0]}
    agent = XSectSkewAgent(config={"skew_lookback": 48}, conn=init_db(":memory:"))
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_max_signal_matches_mean_of_top_returns():
    rets = [0.01, -0.02, 0.03, -0.01, 0.05, -0.04, 0.02, 0.06]
    closes = _from_rets(rets)
    got = XSectSkewAgent._lottery_signal(closes, len(rets), "max", n_max=3)
    expected = sum(sorted(rets, reverse=True)[:3]) / 3
    assert got is not None
    assert abs(got - expected) < 1e-12


def test_skew_signal_is_signed_correctly():
    # right-skewed: many small negatives, one large positive -> positive skew
    right = _from_rets([-0.01] * 9 + [0.20])
    # symmetric alternating -> ~zero skew
    symm = _from_rets([(0.02 if i % 2 else -0.02) for i in range(10)])
    s_right = XSectSkewAgent._lottery_signal(right, 10, "skew")
    s_symm = XSectSkewAgent._lottery_signal(symm, 10, "skew")
    assert s_right is not None and s_symm is not None
    assert s_right > 0.5            # clearly positively skewed
    assert abs(s_symm) < 1e-6       # symmetric -> ~0


def test_skew_signal_shorts_most_right_skewed():
    closes = {
        "LEFT": _from_rets([0.01] * 9 + [-0.20]),    # negative skew -> long
        "SYMM": _from_rets([(0.02 if i % 2 else -0.02) for i in range(10)]),
        "MILD": _from_rets([-0.005] * 9 + [0.08]),   # mild positive skew
        "RIGHT": _from_rets([-0.01] * 9 + [0.20]),   # strong positive skew -> short
    }
    agent = XSectSkewAgent(
        config={"skew_lookback": 10, "signal": "skew", "top_k": 1}, conn=init_db(":memory:")
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["RIGHT"].side == "A"   # most right-skewed -> short
    assert decs["LEFT"].side == "B"    # most left-skewed -> long


def test_degenerate_skew_returns_none():
    flat = [100.0] * 12          # zero dispersion -> skew undefined
    assert XSectSkewAgent._lottery_signal(flat, 10, "skew") is None


def test_bad_signal_rejected():
    with pytest.raises(ValueError):
        XSectSkewAgent(config={"signal": "bogus"}, conn=init_db(":memory:"))
