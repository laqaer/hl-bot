"""Confirmation tests for the session-timing (clock-time seasonality) strategy.

Session-timing is the first agent whose signal reads *neither* price nor funding:
it keys only off the bar's UTC clock time (hour-of-day + weekday from ``ts_ms``).
We check the pure window predicate and the entry/exit behaviour:
  * ``in_session`` is true only inside the a-priori US-session hour band, false
    overnight, and false on weekends when ``weekdays_only``;
  * ``invert`` trades the exact complement;
  * a midnight-wrapping window is handled;
  * inside the session the agent LONGs the eligible liquid universe (no price
    input), illiquid coins are filtered;
  * outside the session it flattens whatever it holds and otherwise holds flat.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hl_bot.agents.base import MarketView
from hl_bot.agents.session_timing import SessionTimingAgent, in_session
from hl_bot.db.schema import init_db


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


# 2026-06-10 is a Wednesday; 2026-06-13 is a Saturday.
WED = (2026, 6, 10)
SAT = (2026, 6, 13)


def test_in_session_true_inside_us_hours_weekday():
    assert in_session(_ts(*WED, 15), 14, 21) is True
    assert in_session(_ts(*WED, 14), 14, 21) is True   # inclusive lower edge
    assert in_session(_ts(*WED, 20), 14, 21) is True


def test_in_session_false_outside_hours_and_upper_edge_exclusive():
    assert in_session(_ts(*WED, 3), 14, 21) is False
    assert in_session(_ts(*WED, 21), 14, 21) is False  # exclusive upper edge
    assert in_session(_ts(*WED, 23), 14, 21) is False


def test_in_session_false_on_weekend_when_weekdays_only():
    assert in_session(_ts(*SAT, 15), 14, 21, weekdays_only=True) is False
    assert in_session(_ts(*SAT, 15), 14, 21, weekdays_only=False) is True


def test_invert_is_exact_complement():
    for hour in range(24):
        base = in_session(_ts(*WED, hour), 14, 21)
        assert in_session(_ts(*WED, hour), 14, 21, invert=True) is (not base)


def test_window_wraps_midnight():
    # 22:00 -> 04:00 overnight band
    assert in_session(_ts(*WED, 23), 22, 4) is True
    assert in_session(_ts(*WED, 2), 22, 4) is True
    assert in_session(_ts(*WED, 12), 22, 4) is False


def _view(ts_ms: int, vol: dict[str, float]) -> MarketView:
    mids = {c: 100.0 for c in vol}
    return MarketView(ts_ms=ts_ms, mids=mids, extra={"day_ntl_vlm": vol})


def test_enters_eligible_universe_in_session_long():
    agent = SessionTimingAgent(conn=init_db(":memory:"))
    vol = {"BTC": 5e8, "ETH": 5e8, "THIN": 1e6}  # THIN below volume gate
    decs = [d for d in agent.decide(_view(_ts(*WED, 15), vol)) if d.action == "place"]
    coins = {d.coin: d.side for d in decs}
    assert coins == {"BTC": "B", "ETH": "B"}     # long, illiquid filtered out


def test_holds_flat_outside_session():
    agent = SessionTimingAgent(conn=init_db(":memory:"))
    vol = {"BTC": 5e8, "ETH": 5e8}
    decs = agent.decide(_view(_ts(*WED, 3), vol))
    assert all(d.action == "hold" for d in decs)


def test_flattens_held_position_when_session_closes():
    conn = init_db(":memory:")
    agent = SessionTimingAgent(conn=conn)
    vol = {"BTC": 5e8}
    # open inside the session, then evaluate a bar after the session has closed
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, coin, action, side, sz, px)
           VALUES (?,?,?,?,?,?,?)""",
        (_ts(*WED, 15), agent.name, "BTC", "place", "B", 0.1, 100.0),
    )
    conn.commit()
    decs = agent.decide(_view(_ts(*WED, 23), vol))
    flat = [d for d in decs if d.action == "flatten"]
    assert len(flat) == 1 and flat[0].coin == "BTC"
