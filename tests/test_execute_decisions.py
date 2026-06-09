"""Tests for the live order-placement loop (``runtime.execute_decisions``).

This loop was previously inlined and untested inside ``cli.femr_tick`` — the
actual code that places real orders (REVIEW M3/D2). Extracting it lets us assert
the safety-critical behavior with a fake exchange and a real in-memory DB:

- guardrail / cooldown / resting-quote gating blocks ``place`` before any order;
- a ``place`` is logged ONLY after the exchange confirms, with the REAL fill
  px/sz (not the pre-trade mid), so stops/TPs and cooldown key off truth;
- a taker reject is logged as ``rejected`` so the coin enters cooldown;
- maker mode rests a post-only quote and never stacks a duplicate.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.agents.cloid import make_cloid
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.runtime import execute_decisions
from hl_bot.db.schema import init_db
from hl_bot.exec import orders

AGENT = "femr_v1"


def _filled(avg_px: float, sz: float, cloid: str | None = None) -> dict:
    return {"response": {"data": {"statuses": [
        {"filled": {"avgPx": str(avg_px), "totalSz": str(sz), "oid": 7, "cloid": cloid}}]}}}


def _rejected(reason: str = "insufficient margin") -> dict:
    return {"response": {"data": {"statuses": [{"error": reason}]}}}


def _resting(oid: int = 42, cloid: str | None = None) -> dict:
    return {"response": {"data": {"statuses": [{"resting": {"oid": oid, "cloid": cloid}}]}}}


class _FakeInfo:
    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 3}]}


class _FakeExchange:
    def __init__(self, *, market=None, close=None, order=None):
        self.info = _FakeInfo()
        self.market_calls: list[dict] = []
        self.close_calls: list[dict] = []
        self.order_calls: list[dict] = []
        self._market, self._close, self._order = market, close, order

    def market_open(self, **kw):
        self.market_calls.append(kw)
        return self._market

    def market_close(self, **kw):
        self.close_calls.append(kw)
        return self._close

    def order(self, **kw):
        self.order_calls.append(kw)
        return self._order


@pytest.fixture
def conn():
    orders._SZ_DECIMALS_CACHE.pop("BTC", None)
    c = init_db(":memory:")
    yield c
    c.close()


def _view(book_top=None):
    return MarketView(ts_ms=0, mids={"BTC": 100.0}, book_top=book_top or {})


def _place(px=100.0, side="B", sz=0.01):
    # is_paper=False mirrors live: femr_tick sets it before execute_decisions runs.
    return Decision(agent=AGENT, action="place", coin="BTC", side=side, sz=sz,
                    px=px, cloid=make_cloid(AGENT), reasoning="entry", is_paper=False)


def _logged(conn, action):
    return conn.execute(
        "SELECT coin, px, sz, is_paper FROM agent_decisions WHERE agent=? AND action=?",
        (AGENT, action),
    ).fetchall()


def test_taker_fill_logs_real_fill_price_not_pretrade_mid(conn):
    ex = _FakeExchange(market=_filled(101.5, 0.01))
    events = execute_decisions(conn, ex, _view(), [_place(px=100.0)],
                               agent_names={AGENT}, guardrails_ok=True)
    assert [e.kind for e in events] == ["filled"]
    assert len(ex.market_calls) == 1
    rows = _logged(conn, "place")
    assert len(rows) == 1
    # Logged with the REAL fill price (101.5), not the pre-trade mid (100.0).
    assert rows[0]["px"] == pytest.approx(101.5)
    assert rows[0]["sz"] == pytest.approx(0.01)
    assert rows[0]["is_paper"] == 0


def test_taker_reject_logs_rejected_and_triggers_cooldown(conn):
    ex = _FakeExchange(market=_rejected())
    events = execute_decisions(conn, ex, _view(), [_place()],
                               agent_names={AGENT}, guardrails_ok=True)
    assert [e.kind for e in events] == ["reject"]
    assert _logged(conn, "rejected")           # rejection recorded
    assert not _logged(conn, "place")          # no phantom ownership
    assert orders.coin_in_cooldown(conn, "BTC", agent=AGENT)


def test_guardrail_block_skips_place_without_touching_exchange(conn):
    ex = _FakeExchange(market=_filled(100.0, 0.01))
    events = execute_decisions(conn, ex, _view(), [_place()],
                               agent_names={AGENT}, guardrails_ok=False)
    assert [e.kind for e in events] == ["skip"]
    assert "guardrail" in events[0].message
    assert ex.market_calls == []               # never reached the exchange
    assert not _logged(conn, "place")


def test_cooldown_skips_place(conn):
    log_decision(conn, Decision(agent=AGENT, action="place", coin="BTC",
                                side="B", sz=0.01, px=100.0))
    ex = _FakeExchange(market=_filled(100.0, 0.01))
    events = execute_decisions(conn, ex, _view(), [_place()],
                               agent_names={AGENT}, guardrails_ok=True)
    assert [e.kind for e in events] == ["skip"]
    assert "cooldown" in events[0].message
    assert ex.market_calls == []


def test_maker_rests_post_only_at_touch_and_dedups(conn):
    ex = _FakeExchange(order=_resting(oid=42))
    view = _view(book_top={"BTC": (100.0, 100.2)})
    d = _place(side="B")
    events = execute_decisions(conn, ex, view, [d], agent_names={AGENT},
                               guardrails_ok=True, execution="maker")
    assert [e.kind for e in events] == ["resting"]
    # Joined the near touch (best bid) for a buy — never crosses.
    assert ex.order_calls[0]["limit_px"] == pytest.approx(100.0)
    assert ex.order_calls[0]["order_type"] == {"limit": {"tif": "Alo"}}
    assert ex.market_calls == []               # maker never sends a market order

    # A second decision on the same coin must not stack a duplicate quote.
    events2 = execute_decisions(conn, ex, view, [_place(side="B")],
                                agent_names={AGENT}, guardrails_ok=True,
                                execution="maker")
    assert [e.kind for e in events2] == ["skip"]
    assert "resting" in events2[0].message
    assert len(ex.order_calls) == 1


def test_flatten_closes_and_logs_real_exit_price(conn):
    ex = _FakeExchange(close=_filled(99.0, 0.01))
    d = Decision(agent=AGENT, action="flatten", coin="BTC", cloid=make_cloid(AGENT))
    events = execute_decisions(conn, ex, _view(), [d],
                               agent_names={AGENT}, guardrails_ok=True)
    assert [e.kind for e in events] == ["closed"]
    assert len(ex.close_calls) == 1
    rows = _logged(conn, "flatten")
    assert len(rows) == 1 and rows[0]["px"] == pytest.approx(99.0)


def test_foreign_agent_and_missing_coin_are_ignored(conn):
    ex = _FakeExchange(market=_filled(100.0, 0.01))
    decisions = [
        _place(),                                                   # agent not in set
        Decision(agent=AGENT, action="place", side="B", sz=0.01),   # coin is None
    ]
    events = execute_decisions(conn, ex, _view(), decisions,
                               agent_names={"other_agent"}, guardrails_ok=True)
    assert events == []
    assert ex.market_calls == []
