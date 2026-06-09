"""Confirmation tests for the cross-sectional low-volatility strategy.

Betting-against-volatility = LONG the calmest names / SHORT the wildest. We build
synthetic close paths with a deterministic realized-vol ranking and check:
  * it LONGs the lowest-vol coins and SHORTs the highest-vol coins;
  * the ``invert`` flag flips both legs (long high-vol, short low-vol);
  * it needs at least ``2*top_k`` eligible coins to form both legs, else holds;
  * thin (sub-volume) coins are filtered out;
  * a too-short series yields a hold;
  * realized vol is computed from log-returns and ranks coins correctly.
"""

from __future__ import annotations

import math

from hl_bot.agents.base import MarketView
from hl_bot.agents.xsect_lowvol import XSectLowVolAgent
from hl_bot.db.schema import init_db


def _view(closes: dict[str, list[float]], vol: dict[str, float] | float = 5e7) -> MarketView:
    mids = {c: s[-1] for c, s in closes.items()}
    vmap = vol if isinstance(vol, dict) else {c: vol for c in closes}
    return MarketView(
        ts_ms=0, mids=mids,
        extra={"closes": closes, "day_ntl_vlm": vmap},
    )


def _walk(start: float, step: float, n: int = 60) -> list[float]:
    """n closes alternating +/-step each bar -> realized vol scales with `step`."""
    out = [start]
    for i in range(1, n):
        out.append(out[-1] * (1 + (step if i % 2 else -step)))
    return out


def test_longs_calmest_shorts_wildest():
    closes = {
        "CALM": _walk(100.0, 0.002),   # tiny oscillation -> low vol
        "MILD": _walk(100.0, 0.010),
        "WILD": _walk(100.0, 0.050),   # big oscillation -> high vol
        "INSANE": _walk(100.0, 0.090),
    }
    agent = XSectLowVolAgent(
        config={"vol_lookback": 48, "top_k": 1}, conn=init_db(":memory:")
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    assert decs["CALM"].side == "B"      # calmest -> long
    assert decs["INSANE"].side == "A"    # wildest -> short
    # with top_k=1 the middle names are untouched
    assert "MILD" not in decs and "WILD" not in decs


def test_invert_flag_flips_both_legs():
    closes = {
        "CALM": _walk(100.0, 0.002),
        "WILD": _walk(100.0, 0.090),
    }
    agent = XSectLowVolAgent(
        config={"vol_lookback": 48, "top_k": 1, "invert": True},
        conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes)) if d.action == "place"}
    # invert: long the wild one, short the calm one
    assert decs["WILD"].side == "B"
    assert decs["CALM"].side == "A"


def test_needs_enough_coins_for_both_legs():
    # top_k=2 needs >=4 eligible coins; only 3 -> hold.
    closes = {
        "A": _walk(100.0, 0.002),
        "B": _walk(100.0, 0.020),
        "C": _walk(100.0, 0.060),
    }
    agent = XSectLowVolAgent(config={"vol_lookback": 48, "top_k": 2}, conn=init_db(":memory:"))
    actions = {d.action for d in agent.decide(_view(closes))}
    assert actions == {"hold"}


def test_thin_coins_filtered_out():
    closes = {
        "CALM": _walk(100.0, 0.002),
        "WILD": _walk(100.0, 0.090),
        "THIN": _walk(100.0, 0.001),  # would be the calmest, but no volume
    }
    vol = {"CALM": 5e7, "WILD": 5e7, "THIN": 1.0}
    agent = XSectLowVolAgent(config={"vol_lookback": 48, "top_k": 1}, conn=init_db(":memory:"))
    decs = {d.coin: d for d in agent.decide(_view(closes, vol)) if d.action == "place"}
    assert "THIN" not in decs
    assert decs["CALM"].side == "B"  # CALM is now the long despite THIN being calmer


def test_short_series_holds():
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 49.0, 48.0]}
    agent = XSectLowVolAgent(config={"vol_lookback": 48}, conn=init_db(":memory:"))
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_realized_vol_matches_manual_log_return_std():
    closes = _walk(100.0, 0.030, n=20)
    v = XSectLowVolAgent._realized_vol(closes, 19)
    window = closes[-20:]
    rets = [math.log(b / a) for a, b in zip(window, window[1:], strict=False)]
    mean = sum(rets) / len(rets)
    expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    assert v is not None
    assert abs(v - expected) < 1e-12


def test_higher_step_has_higher_realized_vol():
    calm = XSectLowVolAgent._realized_vol(_walk(100.0, 0.005), 48)
    wild = XSectLowVolAgent._realized_vol(_walk(100.0, 0.050), 48)
    assert calm is not None and wild is not None
    assert wild > calm
