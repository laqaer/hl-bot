"""S5 cross-venue funding signal — pure logic tests (no network).

Covers the named hazard (symbol mapping: kPEPE vs 1000PEPE), unit normalization
(per-8h -> per-hour -> bps/8h), the directional fail-open entry filter, and
parsing of synthetic Binance/Bybit payloads.
"""

from __future__ import annotations

import pytest

from hl_bot.research.funding_xvenue import (
    consensus_per_hr,
    fetch_xvenue_funding,
    hl_to_binance,
    hl_to_bybit,
    merge_xvenue,
    parse_binance_premium_index,
    parse_bybit_tickers,
    passes_xvenue_filter,
    per_hr_from_interval,
    per_hr_to_bps_8h,
    xvenue_spread_per_hr,
)

# ---- symbol mapping (the hazard) ------------------------------------------

def test_mapping_majors_pass_through():
    assert hl_to_binance("BTC") == "BTCUSDT"
    assert hl_to_bybit("ETH") == "ETHUSDT"


def test_mapping_k_prefix_table():
    # Explicit overrides for the known 1000x meme tokens.
    assert hl_to_binance("kPEPE") == "1000PEPEUSDT"
    assert hl_to_bybit("kBONK") == "1000BONKUSDT"
    assert hl_to_binance("kSHIB") == "1000SHIBUSDT"


def test_mapping_k_prefix_general_rule():
    # A new HL kXYZ listing maps without a code change via the general rule.
    assert hl_to_binance("kXYZ") == "1000XYZUSDT"


def test_mapping_lowercase_k_word_not_misfired():
    # Only a lowercase 'k' followed by an UPPER token triggers the 1000 rule.
    assert hl_to_binance("kit".upper()) == "KITUSDT"  # "KIT" has no leading 'k'


def test_hl_native_coins_have_no_offvenue_symbol():
    assert hl_to_binance("HYPE") is None
    assert hl_to_bybit("PURR") is None


# ---- unit normalization ----------------------------------------------------

def test_per_hr_from_interval_default_8h():
    assert per_hr_from_interval(0.0008) == 0.0001  # 0.08%/8h -> 0.01%/hr


def test_per_hr_to_bps_8h_roundtrips_units():
    # 0.0001/hr == 0.0008/8h == 8 bps/8h
    assert per_hr_to_bps_8h(0.0001) == 8.0


# ---- consensus + spread ----------------------------------------------------

def test_consensus_means_available_venues():
    assert consensus_per_hr({"binance": 0.0001, "bybit": 0.0003}) == pytest.approx(0.0002)


def test_consensus_skips_missing_and_empty():
    assert consensus_per_hr({"binance": 0.0001, "bybit": None}) == 0.0001
    assert consensus_per_hr({}) is None
    assert consensus_per_hr(None) is None


def test_spread_is_hl_minus_consensus():
    assert xvenue_spread_per_hr(0.0005, {"binance": 0.0001, "bybit": 0.0003}) == pytest.approx(0.0003)
    assert xvenue_spread_per_hr(0.0005, {}) is None


# ---- the entry filter (directional, fail-open) -----------------------------

def test_filter_allows_when_hl_richer_than_consensus_short_side():
    # HL +0.0005/hr, consensus +0.0002/hr -> spread +0.0003/hr = 24 bps/8h.
    xv = {"binance": 0.0002, "bybit": 0.0002}
    assert passes_xvenue_filter(0.0005, xv, min_spread_bps_8h=20.0) is True
    assert passes_xvenue_filter(0.0005, xv, min_spread_bps_8h=30.0) is False


def test_filter_blocks_when_hl_only_matches_consensus():
    # HL == consensus -> zero idiosyncratic spread -> not worth taking.
    xv = {"binance": 0.0005, "bybit": 0.0005}
    assert passes_xvenue_filter(0.0005, xv, min_spread_bps_8h=10.0) is False


def test_filter_is_directional_for_negative_funding_long_side():
    # Long collects negative funding; HL more negative than consensus passes.
    xv = {"binance": -0.0002, "bybit": -0.0002}
    assert passes_xvenue_filter(-0.0005, xv, min_spread_bps_8h=20.0) is True
    # HL less negative than consensus (wrong direction) fails.
    assert passes_xvenue_filter(-0.0001, xv, min_spread_bps_8h=5.0) is False


def test_filter_fails_open_without_consensus():
    # No cross-venue data must never silently halt trading.
    assert passes_xvenue_filter(0.0009, {}, min_spread_bps_8h=50.0) is True
    assert passes_xvenue_filter(0.0009, None, min_spread_bps_8h=50.0) is True


# ---- payload parsing -------------------------------------------------------

def test_parse_binance_premium_index_maps_and_normalizes():
    rows = [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0008"},
        {"symbol": "1000PEPEUSDT", "lastFundingRate": "0.0016"},
        {"symbol": "ZZZUSDT", "lastFundingRate": "0.5"},  # not requested -> dropped
    ]
    out = parse_binance_premium_index(rows, ["BTC", "kPEPE"])
    assert out == {"BTC": 0.0001, "kPEPE": 0.0002}


def test_parse_binance_handles_garbage():
    assert parse_binance_premium_index("nope", ["BTC"]) == {}
    bad = [{"symbol": "BTCUSDT", "lastFundingRate": "n/a"}, "x", {}]
    assert parse_binance_premium_index(bad, ["BTC"]) == {}


def test_parse_bybit_tickers_maps_and_normalizes():
    payload = {"result": {"list": [
        {"symbol": "ETHUSDT", "fundingRate": "0.0008"},
        {"symbol": "1000BONKUSDT", "fundingRate": ""},  # empty -> skipped
    ]}}
    out = parse_bybit_tickers(payload, ["ETH", "kBONK"])
    assert out == {"ETH": 0.0001}


def test_parse_bybit_handles_garbage():
    assert parse_bybit_tickers({}, ["ETH"]) == {}
    assert parse_bybit_tickers({"result": {"list": "x"}}, ["ETH"]) == {}


def test_merge_xvenue_combines_present_venues():
    merged = merge_xvenue({"BTC": 0.0001}, {"BTC": 0.0002, "ETH": 0.0003})
    assert merged == {
        "BTC": {"binance": 0.0001, "bybit": 0.0002},
        "ETH": {"bybit": 0.0003},
    }


def test_fetch_is_network_guarded(monkeypatch):
    # With httpx unimportable / failing, fetch returns an empty map, never raises.
    import hl_bot.research.funding_xvenue as mod

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("no network")

    monkeypatch.setattr(mod, "_BINANCE_URL", "http://127.0.0.1:0/x")
    monkeypatch.setattr(mod, "_BYBIT_URL", "http://127.0.0.1:0/x")
    import httpx

    monkeypatch.setattr(httpx, "Client", boom)
    assert fetch_xvenue_funding(["BTC"]) == {}
