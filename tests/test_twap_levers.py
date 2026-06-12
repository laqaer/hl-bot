"""Tests for twap_mr_v1's opt-in improvement levers (default OFF → proven baseline).

  * regime_filter: drops fades that lean against a strong local trend.
  * size_by_signal: scales capital by signal strength (weak fades get less).
  * funding_filter: skips fades that adverse funding would tax while held.
All are experiments the loop A/B-validates on real data before flipping live.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.twap_mr import TwapMrAgent, _signal_size_mult, funding_allows_fade
from hl_bot.db.schema import init_db


def _view(
    coins: dict[str, dict],
    closes: dict[str, list[float]] | None = None,
    funding: dict[str, float] | None = None,
) -> MarketView:
    # coins: name -> {mid, vwap, sigma}
    return MarketView(
        ts_ms=0,
        mids={c: v["mid"] for c, v in coins.items()},
        funding=funding or {},
        extra={
            "candles_1h": {c: {"vwap": v["vwap"], "sigma": v["sigma"], "n": 60} for c, v in coins.items()},
            "day_ntl_vlm": {c: 5e7 for c in coins},
            "closes": closes or {},
        },
    )


def test_signal_size_mult_floor_and_full():
    assert _signal_size_mult(2.0, 2.0, 0.5) == 0.5     # marginal -> floor
    assert _signal_size_mult(4.0, 2.0, 0.5) == 1.0     # strong (≥2x) -> full
    assert _signal_size_mult(3.0, 2.0, 0.5) == 0.75    # halfway -> midpoint
    assert _signal_size_mult(2.0, 0.0, 0.5) == 1.0     # degenerate enter -> full


def _places(agent):
    return {d.coin: d.sz * d.px for d in agent if d.action == "place"}


def test_regime_filter_blocks_fade_into_trend():
    # mid 104 above vwap 100 -> z=+4 -> short signal; closes a strong uptrend.
    coins = {"TST": {"mid": 104.0, "vwap": 100.0, "sigma": 1.0}}
    closes = {"TST": [100.0 + i * 0.5 for i in range(30)]}  # consistent +15% uptrend
    view = _view(coins, closes)

    baseline = TwapMrAgent(config={}, conn=init_db(":memory:")).decide(view)
    assert "TST" in _places(baseline)                       # default: fades the trend

    filtered = TwapMrAgent(config={"regime_filter": True}, conn=init_db(":memory:")).decide(view)
    assert "TST" not in _places(filtered)                   # lever: refuses the short-into-strength


def test_size_by_signal_scales_with_strength():
    coins = {
        "WEAK": {"mid": 102.0, "vwap": 100.0, "sigma": 1.0},    # z=+2 (marginal)
        "STRONG": {"mid": 104.0, "vwap": 100.0, "sigma": 1.0},  # z=+4 (strong)
    }
    view = _view(coins)

    off = _places(TwapMrAgent(config={}, conn=init_db(":memory:")).decide(view))
    assert abs(off["WEAK"] - off["STRONG"]) < 1.0          # baseline: ~equal notional

    on = _places(TwapMrAgent(config={"size_by_signal": True}, conn=init_db(":memory:")).decide(view))
    assert on["STRONG"] > on["WEAK"] * 1.5                  # weak fade gets ~half the capital


def test_funding_allows_fade_directionality():
    thr = 5e-5
    # short (z>0) pays when rate < 0
    assert not funding_allows_fade(+3.0, -1e-4, thr)        # adverse beyond threshold -> block
    assert funding_allows_fade(+3.0, -4e-5, thr)            # adverse but within threshold -> allow
    assert funding_allows_fade(+3.0, +3e-4, thr)            # aligned (short collects) -> allow
    # long (z<0) pays when rate > 0
    assert not funding_allows_fade(-3.0, +1e-4, thr)
    assert funding_allows_fade(-3.0, +4e-5, thr)
    assert funding_allows_fade(-3.0, -3e-4, thr)            # aligned (long collects) -> allow
    # unknown rate never blocks (partial live coverage)
    assert funding_allows_fade(+3.0, None, thr)


def test_funding_filter_skips_taxed_fades_only():
    # Both are short signals (mid above vwap). SHORTPAY's funding is strongly
    # negative (a held short pays); SHORTGET's is positive (a held short collects).
    coins = {
        "SHORTPAY": {"mid": 104.0, "vwap": 100.0, "sigma": 1.0},
        "SHORTGET": {"mid": 104.0, "vwap": 100.0, "sigma": 1.0},
    }
    funding = {"SHORTPAY": -2e-4, "SHORTGET": +2e-4}
    view = _view(coins, funding=funding)

    off = _places(TwapMrAgent(config={}, conn=init_db(":memory:")).decide(view))
    assert set(off) == {"SHORTPAY", "SHORTGET"}             # default OFF: baseline unchanged

    on = _places(TwapMrAgent(config={"funding_filter": True}, conn=init_db(":memory:")).decide(view))
    assert set(on) == {"SHORTGET"}                          # lever: only the taxed fade is dropped


def test_funding_filter_missing_rate_never_blocks():
    coins = {"NODATA": {"mid": 96.0, "vwap": 100.0, "sigma": 1.0}}   # z=-4 -> long
    view = _view(coins)                                              # no funding entry at all
    on = _places(TwapMrAgent(config={"funding_filter": True}, conn=init_db(":memory:")).decide(view))
    assert "NODATA" in on
