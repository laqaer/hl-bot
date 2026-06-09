"""Confirmation tests for the cross-sectional illiquidity strategy.

Amihud illiquidity premium = LONG the most-illiquid names / SHORT the most-liquid,
where illiquidity = mean |log-return| / dollar volume (price impact per dollar).
We build synthetic close paths + dollar volumes with a deterministic illiquidity
ranking and check:
  * it LONGs the most-illiquid coins and SHORTs the most-liquid coins;
  * the ``invert`` flag flips both legs (long liquid, short illiquid);
  * it needs at least ``2*top_k`` eligible coins to form both legs, else holds;
  * thin (sub-volume) coins are filtered out;
  * a too-short series yields a hold;
  * the illiquidity measure matches the manual |log-return|/volume formula and
    moves the expected way with both the |return| numerator and the volume
    denominator.
"""

from __future__ import annotations

import math

from hl_bot.agents.base import MarketView
from hl_bot.agents.xsect_illiq import XSectIlliqAgent
from hl_bot.db.schema import init_db


def _view(closes: dict[str, list[float]], vol: dict[str, float] | float = 5e7) -> MarketView:
    mids = {c: s[-1] for c, s in closes.items()}
    vmap = vol if isinstance(vol, dict) else {c: vol for c in closes}
    return MarketView(
        ts_ms=0, mids=mids,
        extra={"closes": closes, "day_ntl_vlm": vmap},
    )


def _walk(start: float, step: float, n: int = 60) -> list[float]:
    """n closes alternating +/-step each bar -> mean |log-return| scales with `step`."""
    out = [start]
    for i in range(1, n):
        out.append(out[-1] * (1 + (step if i % 2 else -step)))
    return out


def test_longs_most_illiquid_shorts_most_liquid():
    # Same price path (same |return|) for all -> illiquidity is ranked purely by
    # inverse dollar volume: lowest volume = most illiquid = long.
    closes = {c: _walk(100.0, 0.010) for c in ("THICK", "MID1", "MID2", "THIN")}
    vol = {"THICK": 9e8, "MID1": 5e8, "MID2": 3e8, "THIN": 2e7}
    agent = XSectIlliqAgent(config={"illiq_lookback": 48, "top_k": 1}, conn=init_db(":memory:"))
    decs = {d.coin: d for d in agent.decide(_view(closes, vol)) if d.action == "place"}
    assert decs["THIN"].side == "B"     # most illiquid (lowest volume) -> long
    assert decs["THICK"].side == "A"    # most liquid (highest volume) -> short
    assert "MID1" not in decs and "MID2" not in decs


def test_invert_flag_flips_both_legs():
    closes = {c: _walk(100.0, 0.010) for c in ("THICK", "THIN")}
    vol = {"THICK": 9e8, "THIN": 2e7}
    agent = XSectIlliqAgent(
        config={"illiq_lookback": 48, "top_k": 1, "invert": True},
        conn=init_db(":memory:"),
    )
    decs = {d.coin: d for d in agent.decide(_view(closes, vol)) if d.action == "place"}
    # invert: long the liquid one, short the illiquid one
    assert decs["THICK"].side == "B"
    assert decs["THIN"].side == "A"


def test_needs_enough_coins_for_both_legs():
    # top_k=2 needs >=4 eligible coins; only 3 -> hold.
    closes = {c: _walk(100.0, 0.010) for c in ("A", "B", "C")}
    vol = {"A": 9e8, "B": 5e8, "C": 2e7}
    agent = XSectIlliqAgent(config={"illiq_lookback": 48, "top_k": 2}, conn=init_db(":memory:"))
    actions = {d.action for d in agent.decide(_view(closes, vol))}
    assert actions == {"hold"}


def test_thin_coins_filtered_out():
    # SUBVOL would be the most illiquid (tiny volume) but is below the volume gate.
    closes = {c: _walk(100.0, 0.010) for c in ("THICK", "MID", "ILLIQ", "SUBVOL")}
    vol = {"THICK": 9e8, "MID": 5e8, "ILLIQ": 2e7, "SUBVOL": 1.0}
    agent = XSectIlliqAgent(config={"illiq_lookback": 48, "top_k": 1}, conn=init_db(":memory:"))
    decs = {d.coin: d for d in agent.decide(_view(closes, vol)) if d.action == "place"}
    assert "SUBVOL" not in decs
    assert decs["ILLIQ"].side == "B"    # ILLIQ is now the long despite SUBVOL being thinner
    assert decs["THICK"].side == "A"


def test_short_series_holds():
    closes = {"A": [100.0, 101.0, 102.0], "B": [50.0, 49.0, 48.0]}
    agent = XSectIlliqAgent(config={"illiq_lookback": 48}, conn=init_db(":memory:"))
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_illiquidity_matches_manual_formula():
    closes = _walk(100.0, 0.030, n=20)
    dollar_vol = 4e8
    il = XSectIlliqAgent._illiquidity(closes, dollar_vol, 19)
    window = closes[-20:]
    abs_rets = [abs(math.log(b / a)) for a, b in zip(window, window[1:], strict=False)]
    expected = (sum(abs_rets) / len(abs_rets)) / dollar_vol
    assert il is not None
    assert abs(il - expected) < 1e-18


def test_illiquidity_rises_with_return_and_falls_with_volume():
    # Same volume, bigger moves -> more illiquid (higher price impact).
    calm = XSectIlliqAgent._illiquidity(_walk(100.0, 0.005), 5e8, 48)
    wild = XSectIlliqAgent._illiquidity(_walk(100.0, 0.050), 5e8, 48)
    assert calm is not None and wild is not None
    assert wild > calm
    # Same path, more volume -> less illiquid.
    thin = XSectIlliqAgent._illiquidity(_walk(100.0, 0.020), 2e7, 48)
    thick = XSectIlliqAgent._illiquidity(_walk(100.0, 0.020), 9e8, 48)
    assert thin is not None and thick is not None
    assert thin > thick


def test_zero_volume_yields_none():
    assert XSectIlliqAgent._illiquidity(_walk(100.0, 0.010), 0.0, 48) is None
