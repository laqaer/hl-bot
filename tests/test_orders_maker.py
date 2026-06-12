"""Tests for the maker (post-only) execution primitive.

The live HL call can't run here, so we test the parts that determine
correctness: HL tick-rule price rounding, and that the order is submitted as
post-only ("Alo") with the rounded price/size — i.e. it can never silently
become a taker.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.cloid import make_cloid
from hl_bot.exec import orders
from hl_bot.exec.orders import place_limit_order, round_price_to


@pytest.mark.parametrize("px,sz_dec,expected", [
    (64000.7, 5, 64001.0),     # 5 sig figs, no decimals room -> integer
    (1.23456, 2, 1.2346),      # 4 decimals allowed
    (0.0123456, 0, 0.012346),  # sub-1: 5 sig figs
    (0.0, 2, 0.0),             # non-positive passthrough
])
def test_round_price_to(px, sz_dec, expected):
    assert round_price_to(px, sz_dec) == pytest.approx(expected)


@pytest.mark.parametrize("px,sz_dec,direction,expected", [
    # The crossing scenario: bid 12.3456 / ask 12.3458, 3 decimals allowed.
    # Nearest would quote a buy at 12.346 (> ask -> Alo reject); floor rests.
    (12.3456, 3, "down", 12.345),
    (12.3456, 3, "up", 12.346),
    (64000.7, 5, "down", 64000.0),   # integer-tick book: buy floors
    (64000.2, 5, "up", 64001.0),     # sell ceils
    (0.0123456, 0, "down", 0.012345),
    (0.0123451, 0, "up", 0.012346),
    (12.346, 3, "down", 12.346),     # on-tick price unchanged (float repr snap)
    (12.346, 3, "up", 12.346),
])
def test_round_price_to_side_aware(px, sz_dec, direction, expected):
    assert round_price_to(px, sz_dec, direction=direction) == pytest.approx(expected)


def test_buy_quote_rounding_never_crosses_tight_spread():
    # rounding a buy at the bid must never produce a price above the ask
    bid, ask = 12.3456, 12.3458
    assert round_price_to(bid, 3, direction="down") <= ask


class _FakeInfo:
    def meta(self):
        return {"universe": [{"name": "TST", "szDecimals": 2}]}


class _FakeExchange:
    def __init__(self):
        self.info = _FakeInfo()
        self.calls = []

    def order(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": {"data": {"statuses": [{"resting": {"oid": 1, "cloid": kwargs.get("cloid")}}]}}}


def test_place_limit_order_is_post_only_and_rounded():
    orders._SZ_DECIMALS_CACHE.pop("TST", None)
    ex = _FakeExchange()
    res = place_limit_order(ex, "TST", is_buy=True, sz=12.3456, limit_px=1.23456,
                            post_only=True, cloid=make_cloid("twap_mr_regime_v1"))
    assert res.status == "resting"           # rested as maker, not filled
    assert not res.ok                         # not a taker fill
    call = ex.calls[0]
    assert call["order_type"] == {"limit": {"tif": "Alo"}}
    assert call["is_buy"] is True
    assert call["limit_px"] == pytest.approx(1.2345)   # tick-rounded DOWN (buy never crosses)
    assert call["sz"] == pytest.approx(12.34)          # szDecimals=2 floor


def test_place_limit_order_gtc_when_not_post_only():
    orders._SZ_DECIMALS_CACHE.pop("TST", None)
    ex = _FakeExchange()
    place_limit_order(ex, "TST", is_buy=False, sz=10.0, limit_px=2.0,
                      post_only=False, cloid=make_cloid("twap_mr_regime_v1"))
    assert ex.calls[0]["order_type"] == {"limit": {"tif": "Gtc"}}


def test_place_limit_order_requires_cloid():
    ex = _FakeExchange()
    res = place_limit_order(ex, "TST", is_buy=True, sz=1.0, limit_px=1.0, cloid=None)
    assert not res.ok and "cloid" in (res.error or "")


def test_resolve_trader_address(monkeypatch):
    from hl_bot.exec.orders import _resolve_trader_address
    monkeypatch.delenv("HL_TRADER_ADDRESS", raising=False)
    monkeypatch.delenv("HL_ADDRESS", raising=False)
    assert _resolve_trader_address() == ""   # no legacy default: fail loudly
    monkeypatch.setenv("HL_ADDRESS", "0x" + "a" * 40)
    assert _resolve_trader_address() == "0x" + "a" * 40      # falls back to HL_ADDRESS
    monkeypatch.setenv("HL_TRADER_ADDRESS", "0x" + "b" * 40)
    assert _resolve_trader_address() == "0x" + "b" * 40      # HL_TRADER_ADDRESS wins
