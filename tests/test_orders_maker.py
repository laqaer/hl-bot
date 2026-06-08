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
    assert call["limit_px"] == pytest.approx(1.2346)   # tick-rounded
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


def test_maker_limit_price():
    from hl_bot.exec.orders import maker_limit_price
    assert maker_limit_price(99.0, 101.0, True, 100.0) == 99.0     # buy joins best bid
    assert maker_limit_price(99.0, 101.0, False, 100.0) == 101.0   # sell joins best ask
    assert maker_limit_price(None, None, True, 100.0) == 100.0     # no book -> fallback mid
    assert maker_limit_price(0, 0, False, 50.0) == 50.0            # bad book -> fallback
    assert maker_limit_price(101.0, 99.0, True, 100.0) == 100.0    # crossed -> fallback (never cross)


def test_resolve_trader_address(monkeypatch):
    from hl_bot.exec.orders import _resolve_trader_address
    monkeypatch.delenv("HL_TRADER_ADDRESS", raising=False)
    monkeypatch.delenv("HL_ADDRESS", raising=False)
    assert _resolve_trader_address().startswith("0x5C3a")   # legacy default
    monkeypatch.setenv("HL_ADDRESS", "0x" + "a" * 40)
    assert _resolve_trader_address() == "0x" + "a" * 40      # falls back to HL_ADDRESS
    monkeypatch.setenv("HL_TRADER_ADDRESS", "0x" + "b" * 40)
    assert _resolve_trader_address() == "0x" + "b" * 40      # HL_TRADER_ADDRESS wins
