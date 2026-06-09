"""Canonical named-basket resolution for reproducible backtests.

Every recorded confirm/backtest number is only honest if its universe is known.
``resolve_basket`` pins the baskets the edge search uses (majors, alts, …) to
version-controlled names while staying backward compatible with bare symbols.
"""

from __future__ import annotations

from hl_bot.backtest.baskets import (
    BASKETS,
    PAIR_BASKETS,
    coins_in_pairs,
    leave_one_pair_out,
    resolve_basket,
    resolve_pairs,
)


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


# ---- pair baskets (relative-value universe; B-pairs slice 3) ----


def test_resolve_pairs_expands_named_basket():
    assert resolve_pairs("pairs_heldout") == "ARB/OP|APT/SUI|DOGE/WIF"


def test_resolve_pairs_bare_spec_roundtrips_uppercased():
    # The pre-existing agent syntax is preserved (legs upper-cased).
    assert resolve_pairs("eth/btc|sol/avax") == "ETH/BTC|SOL/AVAX"


def test_resolve_pairs_dedupes_and_mixes_basket_with_bare():
    # pairs_default plus an extra bare pair; ETH/BTC already present is not repeated.
    out = resolve_pairs("pairs_default|ETH/BTC|UNI/AAVE")
    assert out == "ETH/BTC|SOL/AVAX|LINK/AAVE|UNI/AAVE"


def test_resolve_pairs_skips_malformed_and_self_pairs():
    assert resolve_pairs("BTC|ETH/ETH|SOL/AVAX|") == "SOL/AVAX"


def test_held_out_pairs_are_disjoint_from_default():
    default = set(coins_in_pairs("pairs_default"))
    heldout = set(coins_in_pairs("pairs_heldout"))
    assert default.isdisjoint(heldout)


def test_coins_in_pairs_flattens_legs_order_preserving():
    assert coins_in_pairs("pairs_heldout") == ["ARB", "OP", "APT", "SUI", "DOGE", "WIF"]


def test_resolve_basket_expands_pair_basket_to_legs():
    # --coins pairs_heldout fetches exactly the pair legs, deduped/order-preserving.
    assert resolve_basket("pairs_heldout") == ["ARB", "OP", "APT", "SUI", "DOGE", "WIF"]


def test_pair_basket_values_are_canonical():
    # Every shipped pair basket already resolves to itself (no drift / typos).
    for spec in PAIR_BASKETS.values():
        assert resolve_pairs(spec) == spec


def test_leave_one_pair_out_drops_each_pair():
    # For the 3-pair default basket: one (dropped, remaining) per pair, each
    # remaining spec the canonical 2-pair complement (order-preserving).
    assert leave_one_pair_out("pairs_default") == [
        ("ETH/BTC", "SOL/AVAX|LINK/AAVE"),
        ("SOL/AVAX", "ETH/BTC|LINK/AAVE"),
        ("LINK/AAVE", "ETH/BTC|SOL/AVAX"),
    ]


def test_leave_one_pair_out_resolves_and_dedupes_input():
    # Input is resolved/uppercased first, so bare/dup specs behave canonically.
    assert leave_one_pair_out("eth/btc|sol/avax|eth/btc") == [
        ("ETH/BTC", "SOL/AVAX"),
        ("SOL/AVAX", "ETH/BTC"),
    ]


def test_leave_one_pair_out_needs_two_pairs():
    # Nothing to leave out of a single pair (or an empty spec).
    assert leave_one_pair_out("ETH/BTC") == []
    assert leave_one_pair_out("") == []
