"""Tests for paper-only candidate strategy scaffolding.

The first candidate is a trend/regime filter that tells the TWAP mean-reversion
agent when NOT to fade: fading a strong directional breakout is exactly the loss
loop we want to avoid. These candidates are paper-only and never wired to live
without passing explicit promotion gates.
"""

from __future__ import annotations

from hl_bot.research.candidates import (
    CANDIDATES,
    is_trending,
    regime_allows_fade,
)


def test_all_registered_candidates_are_paper_and_gated():
    assert CANDIDATES, "expected at least one candidate strategy"
    for c in CANDIDATES:
        assert c.mode == "paper"
        assert c.enabled_live is False
        assert c.promotion_gate, "candidate must declare promotion gates"


def test_is_trending_detects_strong_directional_run():
    up = [1.0 + 0.01 * i for i in range(12)]   # steady ~11% climb
    assert is_trending(up) is True

    choppy = [1.0, 1.01, 0.99, 1.005, 0.995, 1.0, 1.004, 0.996]
    assert is_trending(choppy) is False


def test_regime_blocks_fading_into_an_uptrend():
    up = [1.0 + 0.01 * i for i in range(12)]
    # z > 0 means price above VWAP -> TWAP would SHORT. In a strong uptrend that
    # short should be blocked.
    allow, _reason = regime_allows_fade(z=3.0, closes=up)
    assert allow is False


def test_regime_allows_fade_in_choppy_market():
    choppy = [1.0, 1.01, 0.99, 1.005, 0.995, 1.0, 1.004, 0.996]
    allow, _reason = regime_allows_fade(z=3.0, closes=choppy)
    assert allow is True


def test_regime_allows_counter_trend_fade_aligned_with_reversion():
    # Strong uptrend but z < 0 (price below VWAP) -> fade would BUY, i.e. with
    # the trend; the anti-trend filter should not block that.
    up = [1.0 + 0.01 * i for i in range(12)]
    allow, _reason = regime_allows_fade(z=-3.0, closes=up)
    assert allow is True
