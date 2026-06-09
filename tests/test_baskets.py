"""Canonical named-basket resolution for reproducible backtests.

Every recorded confirm/backtest number is only honest if its universe is known.
``resolve_basket`` pins the baskets the edge search uses (majors, alts, …) to
version-controlled names while staying backward compatible with bare symbols.
"""

from __future__ import annotations

from hl_bot.backtest.baskets import BASKETS, resolve_basket


def test_bare_symbols_pass_through_unchanged():
    # The pre-existing default behaviour: a plain CSV of symbols is preserved.
    assert resolve_basket("BTC,ETH,SOL") == ["BTC", "ETH", "SOL"]


def test_preset_expands():
    assert resolve_basket("majors") == ["BTC", "ETH", "SOL", "HYPE"]


def test_preset_name_is_case_insensitive():
    assert resolve_basket("MAJORS") == BASKETS["majors"]


def test_mixing_preset_and_symbols_dedupes_order_preserving():
    # majors then DOGE; BTC is already in majors so it is not repeated.
    assert resolve_basket("majors,DOGE,BTC") == ["BTC", "ETH", "SOL", "HYPE", "DOGE"]


def test_symbols_are_uppercased():
    assert resolve_basket("btc,eth") == ["BTC", "ETH"]


def test_empty_and_whitespace_tokens_ignored():
    assert resolve_basket("") == []
    assert resolve_basket("  , BTC , ") == ["BTC"]


def test_two_presets_concatenate_and_dedupe():
    # alts_heldout and majors both contain no overlap with each other except none;
    # AAVE appears once even though only in alts_heldout.
    result = resolve_basket("majors,alts_heldout")
    assert result[:4] == ["BTC", "ETH", "SOL", "HYPE"]
    assert result.count("AAVE") == 1
    # no duplicates anywhere
    assert len(result) == len(set(result))
