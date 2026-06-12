"""Execution router tests — the single live order-routing path (REVIEW M3).

A fake exchange (SDK response shapes, no network) drives the full routing
logic: per-agent maker/taker entries, book-aware maker pricing with mid
fallback, guardrail/cooldown gates, taker exits, and the exact decision rows
that get logged (ground truth only after exchange confirmation).
"""

from __future__ import annotations

import pytest

from hl_bot.agents.decisions import Decision
from hl_bot.db.schema import init_db
from hl_bot.exec.maker import maker_price, working_orders
from hl_bot.exec.orders import bot_owned_coins
from hl_bot.exec.router import execute_decisions

T0 = 1_750_000_000_000


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "r.sqlite")


def _filled(px=100.0, sz=1.0, oid=1):
    return {"response": {"data": {"statuses": [
        {"filled": {"avgPx": str(px), "totalSz": str(sz), "oid": oid}}
    ]}}}


def _resting(oid=7):
    return {"response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _rejected(msg="Post only order would have immediately matched"):
    return {"response": {"data": {"statuses": [{"error": msg}]}}}


class FakeInfo:
    def meta(self):
        return {"universe": [
            {"name": "BTC", "szDecimals": 5},
            {"name": "ETH", "szDecimals": 4},
        ]}


class FakeExchange:
    """Records calls; returns canned HL response shapes."""

    def __init__(self, market_res=None, limit_res=None, close_res=None):
        self.info = FakeInfo()
        self.market_res = market_res or _filled()
        self.limit_res = limit_res or _resting()
        self.close_res = close_res or _filled(px=99.0)
        self.market_calls: list[dict] = []
        self.limit_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def market_open(self, name, is_buy, sz, slippage, cloid, builder=None):
        self.market_calls.append({"coin": name, "is_buy": is_buy, "sz": sz})
        return self.market_res

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only, cloid, builder=None):
        self.limit_calls.append({"coin": name, "is_buy": is_buy, "sz": sz,
                                 "px": limit_px, "tif": order_type["limit"]["tif"]})
        return self.limit_res

    def market_close(self, coin, cloid, builder=None):
        self.close_calls.append({"coin": coin})
        return self.close_res


def _place(agent, coin, side="B", sz=0.01, px=100.0, cloid="0x" + "ab" * 16):
    return Decision(agent=agent, action="place", coin=coin, side=side,
                    sz=sz, px=px, cloid=cloid, is_paper=False)


def _flatten(agent, coin, cloid="0x" + "cd" * 16):
    return Decision(agent=agent, action="flatten", coin=coin, sz=0.01,
                    px=100.0, cloid=cloid, is_paper=False)


# ---------------------------------------------------------------------------
# maker pricing
# ---------------------------------------------------------------------------


def test_maker_price_joins_the_touch():
    book = (99.0, 101.0)
    assert maker_price("B", book, 100.0) == 99.0    # buy at best bid
    assert maker_price("A", book, 100.0) == 101.0   # sell at best ask


def test_maker_price_falls_back_without_fresh_book():
    assert maker_price("B", None, 100.0) == 100.0
    assert maker_price("B", (0.0, 101.0), 100.0) == 100.0   # degenerate book
    assert maker_price("A", (102.0, 101.0), 100.0) == 100.0  # crossed book


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def test_taker_entry_logs_real_fill_px(conn):
    ex = FakeExchange(market_res=_filled(px=100.7, sz=0.009))
    out = execute_decisions(conn, ex, [_place("twap_mr_v1", "BTC", px=100.0)],
                            exec_modes={"twap_mr_v1": "taker"}, entries_allowed=True)
    assert [o.status for o in out] == ["filled"]
    assert ex.market_calls and not ex.limit_calls
    row = conn.execute(
        "SELECT px, sz FROM agent_decisions WHERE agent='twap_mr_v1' AND action='place'"
    ).fetchone()
    assert row["px"] == pytest.approx(100.7)   # real fill px, not the intent px
    assert row["sz"] == pytest.approx(0.009)


def test_maker_entry_rests_at_touch_not_mid(conn):
    ex = FakeExchange(limit_res=_resting(oid=42))
    out = execute_decisions(
        conn, ex, [_place("xfund_carry_v1", "BTC", side="B", px=100.0)],
        exec_modes={"xfund_carry_v1": "maker"}, entries_allowed=True,
        book_top={"BTC": (99.5, 100.5)},
    )
    assert [o.status for o in out] == ["resting"]
    assert ex.limit_calls[0]["px"] == pytest.approx(99.5)   # joined the bid
    assert ex.limit_calls[0]["tif"] == "Alo"                # post-only
    # rested, tracked, but NOT owned until the fill is reconciled
    assert "BTC" in working_orders(conn, "xfund_carry_v1")
    assert bot_owned_coins(conn, "xfund_carry_v1") == set()


def test_maker_entry_falls_back_to_intent_px_without_book(conn):
    ex = FakeExchange(limit_res=_resting())
    execute_decisions(conn, ex, [_place("xfund_carry_v1", "ETH", side="A", px=2500.0)],
                      exec_modes={"xfund_carry_v1": "maker"}, entries_allowed=True)
    assert ex.limit_calls[0]["px"] == pytest.approx(2500.0)


def test_second_maker_quote_on_same_coin_is_skipped(conn):
    ex = FakeExchange(limit_res=_resting())
    d1 = _place("xfund_carry_v1", "BTC", cloid="0x" + "11" * 16)
    out1 = execute_decisions(conn, ex, [d1], exec_modes={"xfund_carry_v1": "maker"},
                             entries_allowed=True)
    assert out1[0].status == "resting"
    # next tick proposes BTC again while the quote is still working — the
    # working-quote gate blocks stacking a duplicate order ('rest' rows are
    # deliberately not in coin_in_cooldown's action set)
    d2 = _place("xfund_carry_v1", "BTC", cloid="0x" + "22" * 16)
    out2 = execute_decisions(conn, ex, [d2], exec_modes={"xfund_carry_v1": "maker"},
                             entries_allowed=True)
    assert out2[0].status == "skipped"
    assert len(ex.limit_calls) == 1


def test_guardrail_blocks_entries_but_not_exits(conn):
    ex = FakeExchange()
    out = execute_decisions(
        conn, ex,
        [_place("femr_v1", "BTC"), _flatten("femr_v1", "ETH")],
        exec_modes={"femr_v1": "maker"}, entries_allowed=False,
    )
    statuses = {(o.action, o.status) for o in out}
    assert ("place", "skipped") in statuses
    assert ("flatten", "closed") in statuses     # risk reduction always allowed
    assert ex.close_calls == [{"coin": "ETH"}]


def test_taker_reject_cools_down_but_maker_reject_only_audits(conn):
    from hl_bot.exec.orders import coin_in_cooldown

    # taker reject -> 'rejected' row, coin enters cooldown
    ex_t = FakeExchange(market_res=_rejected("oops"))
    execute_decisions(conn, ex_t, [_place("twap_mr_v1", "BTC")],
                      exec_modes={"twap_mr_v1": "taker"}, entries_allowed=True)
    assert coin_in_cooldown(conn, "BTC", agent="twap_mr_v1")
    # maker post-only reject ("the touch moved") -> audited under its own
    # action, but the agent may re-quote next tick (no cooldown)
    ex_m = FakeExchange(limit_res=_rejected())
    execute_decisions(conn, ex_m, [_place("xfund_carry_v1", "ETH")],
                      exec_modes={"xfund_carry_v1": "maker"}, entries_allowed=True)
    row = conn.execute(
        "SELECT action FROM agent_decisions WHERE agent='xfund_carry_v1' AND coin='ETH'"
    ).fetchone()
    assert row["action"] == "maker_reject"
    assert not coin_in_cooldown(conn, "ETH", agent="xfund_carry_v1")


def test_agents_off_roster_are_ignored(conn):
    ex = FakeExchange()
    out = execute_decisions(conn, ex, [_place("basis_v1", "BTC")],
                            exec_modes={"femr_v1": "taker"}, entries_allowed=True)
    assert out == []
    assert not ex.market_calls and not ex.limit_calls


def test_flatten_logs_real_exit_px(conn):
    ex = FakeExchange(close_res=_filled(px=98.4))
    execute_decisions(conn, ex, [_flatten("femr_v1", "BTC")],
                      exec_modes={"femr_v1": "taker"}, entries_allowed=True)
    row = conn.execute(
        "SELECT px FROM agent_decisions WHERE action='flatten'"
    ).fetchone()
    assert row["px"] == pytest.approx(98.4)


# ---------------------------------------------------------------------------
# per-agent execution modes
# ---------------------------------------------------------------------------


def test_carry_agents_default_to_maker_momentum_to_taker():
    from hl_bot.agents.funding_carry import FundingCarryAgent
    from hl_bot.agents.twap_mr import TwapMrAgent
    from hl_bot.agents.xfund_carry import XFundCarryAgent

    assert XFundCarryAgent(config={}).execution_mode() == "maker"
    assert FundingCarryAgent(config={}).execution_mode() == "maker"
    assert TwapMrAgent(config={}).execution_mode() == "taker"


def test_execution_mode_config_override_and_garbage_fallback():
    from hl_bot.agents.xfund_carry import XFundCarryAgent

    assert XFundCarryAgent(config={"execution": "taker"}).execution_mode() == "taker"
    assert XFundCarryAgent(config={"execution": "yolo"}).execution_mode() == "maker"
