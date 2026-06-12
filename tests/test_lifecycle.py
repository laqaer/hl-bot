"""Maker lifecycle v2 state machine — pure plan_actions transitions plus the
DB/audit effects of apply_actions with a fake exchange."""

from __future__ import annotations

import time

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.db.schema import init_db
from hl_bot.exec.lifecycle import (
    MakerConfig,
    apply_actions,
    fills_by_cloid,
    open_orders,
    plan_actions,
    price_quote,
    record_quote,
)

NOW = int(time.time() * 1000)
CFG = MakerConfig(reprice_bps=5.0, min_requote_s=30, max_rest_s=900,
                  max_reprices=3, exit_timeout_s=120)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def view(mids=None, book=None):
    return MarketView(ts_ms=NOW, mids=mids or {"BTC": 100.0},
                      book_top=book or {})


def order(conn, *, cloid="c1", agent="a1", coin="BTC", side="B", sz=1.0,
          px=99.0, created_ms=NOW, urgency="normal", reduce_only=False):
    record_quote(conn, cloid=cloid, agent=agent, coin=coin, side=side, sz=sz,
                 limit_px=px, oid=7, urgency=urgency, reduce_only=reduce_only,
                 now_ms=created_ms)
    return open_orders(conn)[-1]


class FakeExchange:
    def __init__(self):
        self.cancelled: list[tuple[str, int]] = []
        self.limit_orders: list[dict] = []
        self.closed: list[str] = []

    def cancel(self, coin, oid):
        self.cancelled.append((coin, oid))
        return {"response": {"data": {"statuses": ["success"]}}}

    def order(self, *, name, is_buy, sz, limit_px, order_type, reduce_only, cloid, builder=None):
        self.limit_orders.append({"coin": name, "is_buy": is_buy, "sz": sz, "px": limit_px})
        return {"response": {"data": {"statuses": [
            {"resting": {"oid": 11, "cloid": str(cloid)}}]}}}

    def market_close(self, coin, cloid=None, builder=None):
        self.closed.append(coin)
        return {"response": {"data": {"statuses": [
            {"filled": {"avgPx": "100.0", "totalSz": "1.0", "oid": 12}}]}}}

    # needed by _round_order_size / _round_price paths
    class info:  # noqa: N801
        @staticmethod
        def meta():
            return {"universe": [{"name": "BTC", "szDecimals": 3}]}


# --- price_quote ---------------------------------------------------------


def test_quote_joins_touch_when_book_present():
    v = view(book={"BTC": (99.5, 100.5)})
    assert price_quote(v, "BTC", "B", CFG) == pytest.approx(99.5)
    assert price_quote(v, "BTC", "A", CFG) == pytest.approx(100.5)


def test_quote_falls_back_inside_mid():
    v = view()
    b = price_quote(v, "BTC", "B", CFG, sz_decimals=3)
    a = price_quote(v, "BTC", "A", CFG, sz_decimals=3)
    assert b < 100.0 < a
    assert price_quote(view(mids={}), "ETH", "B", CFG) is None


# --- plan_actions transitions ---------------------------------------------


def test_full_fill_detected(conn):
    o = order(conn)
    actions = plan_actions([o], {"c1": (1.0, 99.0)}, view(), NOW + 1000, CFG)
    assert [a.kind for a in actions] == ["fill"]
    assert actions[0].filled_sz == 1.0


def test_partial_fill_updates_and_keeps_resting(conn):
    o = order(conn)
    actions = plan_actions([o], {"c1": (0.4, 99.0)}, view(), NOW + 1000, CFG)
    assert actions[0].kind == "partial"
    assert actions[0].filled_sz == pytest.approx(0.4)


def test_reprice_when_touch_drifts(conn):
    o = order(conn, px=99.0)
    # touch moved to 99.9: drift 90 bps > 5 bps, age > min_requote_s
    v = view(book={"BTC": (99.9, 100.1)})
    actions = plan_actions([o], {}, v, NOW + 60_000, CFG)
    assert [a.kind for a in actions] == ["reprice"]
    assert actions[0].new_px == pytest.approx(99.9)


def test_no_reprice_before_min_requote(conn):
    o = order(conn, px=99.0)
    v = view(book={"BTC": (99.9, 100.1)})
    assert plan_actions([o], {}, v, NOW + 5_000, CFG) == []


def test_no_reprice_past_max_reprices(conn):
    o = order(conn, px=99.0)
    o["reprice_count"] = CFG.max_reprices
    v = view(book={"BTC": (99.9, 100.1)})
    assert plan_actions([o], {}, v, NOW + 60_000, CFG) == []


def test_entry_expires_after_max_rest(conn):
    o = order(conn)
    actions = plan_actions([o], {}, view(), NOW + 901_000, CFG)
    assert [a.kind for a in actions] == ["expire"]


def test_exit_escalates_to_taker(conn):
    # px at the (rounded) fallback quote so no reprice noise in this test.
    o = order(conn, urgency="exit", reduce_only=True, px=100.0)
    actions = plan_actions([o], {}, view(), NOW + 121_000, CFG)
    assert [a.kind for a in actions] == ["taker_fallback"]
    # but not before the timeout
    actions = plan_actions([o], {}, view(), NOW + 60_000, CFG)
    assert actions == []


# --- apply_actions side effects --------------------------------------------


def test_apply_fill_logs_ownership(conn):
    o = order(conn)
    ex = FakeExchange()
    apply_actions(conn, ex, plan_actions([o], {"c1": (1.0, 99.0)}, view(), NOW + 1000, CFG))
    row = conn.execute("SELECT state, filled_sz FROM maker_orders WHERE cloid='c1'").fetchone()
    assert row["state"] == "filled"
    audit = conn.execute(
        "SELECT action FROM agent_decisions WHERE agent='a1' ORDER BY id"
    ).fetchall()
    assert [r["action"] for r in audit] == ["rest", "place"]   # resting != owned; fill owns


def test_apply_reprice_cancels_and_requotes(conn):
    o = order(conn, px=99.0)
    ex = FakeExchange()
    v = view(book={"BTC": (99.9, 100.1)})
    events = apply_actions(conn, ex, plan_actions([o], {}, v, NOW + 60_000, CFG),
                           now_ms=NOW + 60_000)
    assert ex.cancelled == [("BTC", 7)]
    assert len(ex.limit_orders) == 1
    assert any("REPRICED" in e for e in events)
    opens = open_orders(conn)
    assert len(opens) == 1
    assert opens[0]["reprice_count"] == 1
    assert opens[0]["parent_cloid"] == "c1"
    old = conn.execute("SELECT state FROM maker_orders WHERE cloid='c1'").fetchone()
    assert old["state"] == "cancelled"


def test_apply_taker_fallback_closes(conn):
    o = order(conn, urgency="exit", reduce_only=True)
    ex = FakeExchange()
    events = apply_actions(conn, ex, plan_actions([o], {}, view(), NOW + 121_000, CFG))
    assert ex.closed == ["BTC"]
    assert any("TAKER-CLOSE" in e for e in events)
    row = conn.execute("SELECT state FROM maker_orders WHERE cloid='c1'").fetchone()
    assert row["state"] == "taker_fallback"


def test_fills_by_cloid_aggregates(conn):
    for i, (sz, px) in enumerate([(0.4, 99.0), (0.6, 99.2)]):
        conn.execute(
            """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz, agent, cloid, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?, '{}')""",
            (f"h{i}", i, NOW, "BTC", "B", px, sz, "a1", "c1"),
        )
    out = fills_by_cloid(conn, ["c1"])
    assert out["c1"][0] == pytest.approx(1.0)
    assert out["c1"][1] == pytest.approx(99.12)


def test_rejected_entries_are_audited(conn):
    # Rejections must leave audit rows — unlogged rejects would be retried
    # every engine cycle invisibly. A post-only cross is a benign
    # 'maker_reject' (rate-limited but NOT cooled down: the agent may requote);
    # a hard failure is 'rejected' and trips the per-coin cooldown.
    from hl_bot.agents.decisions import Decision
    from hl_bot.exec.lifecycle import submit_entry
    from hl_bot.exec.orders import coin_in_cooldown, order_rate_ok

    class PostOnlyRejectExchange(FakeExchange):
        def order(self, **kw):
            return {"response": {"data": {"statuses": [
                {"error": "Post only order would have immediately matched"}]}}}

    class BrokenExchange(FakeExchange):
        def order(self, **kw):
            raise RuntimeError("boom")

    d = Decision(agent="a1", action="place", coin="BTC", side="B", sz=1.0,
                 cloid="0x" + "ab" * 16, is_paper=False)
    event = submit_entry(conn, PostOnlyRejectExchange(), view(), d, CFG, now_ms=NOW)
    assert event.startswith("REJECT")
    row = conn.execute(
        "SELECT action, is_paper, error FROM agent_decisions WHERE agent='a1'"
    ).fetchone()
    assert row["action"] == "maker_reject"
    assert row["is_paper"] == 0
    assert row["error"]
    assert not coin_in_cooldown(conn, "BTC", agent="a1", cooldown_s=3600)
    _, why = order_rate_ok(conn, "a1", max_per_hour=1, now_ms=NOW + 1)
    assert "a1 order rate" in why  # maker_reject counts toward the rate wall

    d2 = Decision(agent="a2", action="place", coin="ETH", side="B", sz=1.0,
                  cloid="0x" + "cd" * 16, is_paper=False)
    event = submit_entry(conn, BrokenExchange(), view({"ETH": 100.0}), d2, CFG, now_ms=NOW)
    assert event.startswith("REJECT")
    row = conn.execute(
        "SELECT action FROM agent_decisions WHERE agent='a2'").fetchone()
    assert row["action"] == "rejected"
    assert coin_in_cooldown(conn, "ETH", agent="a2", cooldown_s=3600)
