"""Tests for the cross-sectional momentum agent — B-EDGE3.

Pure-function coverage for the trailing-return signal, decide() coverage for
ranking / entries / hysteresis exits / guards via the audit-log replay, and an
engine integration run proving the agent longs the cross-sectional winner and
shorts the loser profitably on a synthetic divergence.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.xmom import XMomAgent, trailing_return
from hl_bot.backtest.engine import Backtester, CostModel, Frame, frozen_clock
from hl_bot.db.schema import init_db

HOUR = 3_600_000
VOL5 = 5e7


def _view(ts_ms: int, mids: dict, closes: dict, vol: dict | None = None) -> MarketView:
    return MarketView(ts_ms=ts_ms, mids=mids,
                      extra={"closes": closes,
                             "day_ntl_vlm": vol or {c: VOL5 for c in mids}})


def _agent(cfg: dict | None = None, conn=None) -> XMomAgent:
    base = {"lookback_bars": 10, "top_k": 2, "exit_rank": 4}
    base.update(cfg or {})
    return XMomAgent(config=base, conn=conn or init_db(":memory:"))


def _trend(start: float, pct_per_bar: float, bars: int) -> list[float]:
    return [start * (1 + pct_per_bar) ** i for i in range(bars)]


# Six-coin cross-section with a clean monotone return spread over 10 bars.
def _cross_section(bars: int = 12) -> tuple[dict, dict]:
    slopes = {"W1": 0.010, "W2": 0.006, "M1": 0.001, "M2": -0.001,
              "L2": -0.006, "L1": -0.010}
    closes = {c: _trend(100.0, s, bars) for c, s in slopes.items()}
    mids = {c: cl[-1] for c, cl in closes.items()}
    return mids, closes


# ---------------------------------------------------------------------------
# Pure signal math
# ---------------------------------------------------------------------------


def test_trailing_return_basic_and_skip():
    closes = [100.0] * 5 + [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
    assert abs(trailing_return(closes, 5) - 0.10) < 1e-9          # 100 -> 110
    # skip=2 drops the last two bars: 100 -> 106 over the prior 5
    assert abs(trailing_return(closes, 5, skip=2) - 0.06) < 1e-9


def test_trailing_return_needs_history_and_guards():
    assert trailing_return([100.0] * 10, 10) is None       # need lookback+1
    assert trailing_return([100.0] * 11, 10) == 0.0
    assert trailing_return([100.0] * 12, 10, skip=2) is None
    assert trailing_return([0.0] + [100.0] * 10, 10) is None   # degenerate base
    assert trailing_return([100.0, 101.0], 0) is None      # nonsense lookback
    assert trailing_return([100.0] * 12, 10, skip=-1) is None


# ---------------------------------------------------------------------------
# decide(): ranking + entries
# ---------------------------------------------------------------------------


def test_decide_longs_top_shorts_bottom():
    mids, closes = _cross_section()
    out = _agent().decide(_view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    assert places == {"W1": "B", "W2": "B", "L1": "A", "L2": "A"}


def test_decide_thin_cross_section_holds():
    mids, closes = _cross_section()
    for c in ("M1", "M2", "L2"):   # 3 ranked coins < 2*top_k=4
        mids.pop(c), closes.pop(c)
    out = _agent().decide(_view(20 * HOUR, mids, closes))
    assert all(d.action == "hold" for d in out)
    assert "need ≥4" in out[0].reasoning


def test_decide_min_abs_return_blocks_flat_legs():
    mids, closes = _cross_section()
    out = _agent({"min_abs_return": 0.02}).decide(_view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    # ±0.1%/bar coins are inside the floor; only the strong legs trade
    assert places == {"W1": "B", "W2": "B", "L1": "A", "L2": "A"}
    out = _agent({"min_abs_return": 0.5}).decide(_view(20 * HOUR, mids, closes))
    assert all(d.action == "hold" for d in out)


def test_decide_volume_floor_excludes_coin_from_ranks():
    mids, closes = _cross_section()
    vol = {c: VOL5 for c in mids}
    vol["W1"] = 1e6   # illiquid: drops out of the ranked universe entirely
    out = _agent().decide(_view(20 * HOUR, mids, closes, vol))
    places = {d.coin: d.side for d in out if d.action == "place"}
    assert "W1" not in places
    assert places["M1"] == "B"   # next-strongest is promoted into the top-2


def test_decide_skip_bars_ignores_recent_reversal():
    mids, closes = _cross_section()
    # W1 crashes on the last two bars; with skip=2 the signal still sees the
    # uptrend, without it W1's rank collapses.
    closes["W1"] = closes["W1"][:-2] + [50.0, 50.0]
    mids["W1"] = 50.0
    out = _agent({"lookback_bars": 8, "skip_bars": 2}).decide(
        _view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    assert places["W1"] == "B"
    out = _agent({"lookback_bars": 8}).decide(_view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    assert places.get("W1") == "A"   # unskipped signal shorts the crash


def test_decide_respects_room_and_notional_caps():
    mids, closes = _cross_section()
    out = _agent({"max_concurrent_positions": 2}).decide(
        _view(20 * HOUR, mids, closes))
    assert len([d for d in out if d.action == "place"]) == 2
    out = _agent({"max_total_notional": 30.0, "max_notional_per_trade": 25.0}
                 ).decide(_view(20 * HOUR, mids, closes))
    places = [d for d in out if d.action == "place"]
    # $25 first leg leaves $5 headroom < the $5 floor after it
    assert len(places) == 2 and places[1].market_snapshot["notional"] == 5.0


# ---------------------------------------------------------------------------
# decide(): invert (cross-sectional reversal — long losers / short winners)
# ---------------------------------------------------------------------------


def test_decide_invert_longs_losers_shorts_winners():
    mids, closes = _cross_section()
    out = _agent({"invert": True}).decide(_view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    assert places == {"L1": "B", "L2": "B", "W1": "A", "W2": "A"}
    # audit trail carries the RAW return: the loser-long shows a negative ret
    snap = next(d.market_snapshot for d in out
                if d.action == "place" and d.coin == "L1")
    assert snap["trailing_return"] < 0 and snap["rank"] == 1
    assert "reversal rank" in next(
        d.reasoning for d in out if d.action == "place" and d.coin == "L1")


def test_decide_invert_min_abs_return_floors_raw_magnitude():
    mids, closes = _cross_section()
    out = _agent({"invert": True, "min_abs_return": 0.02}).decide(
        _view(20 * HOUR, mids, closes))
    places = {d.coin: d.side for d in out if d.action == "place"}
    # near-flat M1/M2 are inside the floor either way; strong legs trade
    assert places == {"L1": "B", "L2": "B", "W1": "A", "W2": "A"}
    out = _agent({"invert": True, "min_abs_return": 0.5}).decide(
        _view(20 * HOUR, mids, closes))
    assert all(d.action == "hold" for d in out)


def test_decide_invert_hysteresis_on_signal_rank():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    _seed(conn, "M2", "B", closes["M2"][-1])   # signal rank 3 under invert
    out = _agent({"invert": True, "exit_rank": 4}, conn).decide(
        _view(20 * HOUR, mids, closes))
    assert not [d for d in out if d.action == "flatten"]
    out = _agent({"invert": True, "exit_rank": 2}, conn).decide(
        _view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "RANK-OUT" in flats[0].reasoning


# ---------------------------------------------------------------------------
# decide(): exits (seeded open position in the audit log)
# ---------------------------------------------------------------------------


def _seed(conn, coin: str, side: str, px: float) -> None:
    log_decision(conn, Decision(agent="xmom_v1", action="place", coin=coin,
                                side=side, sz=1.0, px=px, reasoning="seed"))


def test_decide_hysteresis_holds_mid_rank_exits_out_of_band():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    _seed(conn, "M1", "B", closes["M1"][-1])   # long, currently rank 3 of 6
    out = _agent({"exit_rank": 4}, conn).decide(_view(20 * HOUR, mids, closes))
    assert not [d for d in out if d.action == "flatten"]   # 3 ≤ band 4: hold
    out = _agent({"exit_rank": 2}, conn).decide(_view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "RANK-OUT" in flats[0].reasoning


def test_decide_short_hysteresis_ranks_from_bottom():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    _seed(conn, "M2", "A", closes["M2"][-1])   # short, rank 3 from the bottom
    out = _agent({"exit_rank": 4}, conn).decide(_view(20 * HOUR, mids, closes))
    assert not [d for d in out if d.action == "flatten"]
    out = _agent({"exit_rank": 2}, conn).decide(_view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "RANK-OUT" in flats[0].reasoning


def test_decide_stop_loss_exit_beats_rank_hold():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    _seed(conn, "W1", "B", closes["W1"][-1] * 1.10)   # entry 10% above mid
    out = _agent({"stop_loss_pct": 0.05}, conn).decide(
        _view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "STOP" in flats[0].reasoning


def test_decide_max_hold_exit():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    with frozen_clock(10.0):   # seed the entry at t=10s
        _seed(conn, "W1", "B", closes["W1"][-1])
    with frozen_clock(10.0 + 49 * 3600):
        out = _agent({"max_hold_hours": 48.0}, conn).decide(
            _view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "MAX-HOLD" in flats[0].reasoning


def test_decide_no_signal_exit_when_coin_leaves_universe():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    _seed(conn, "W1", "B", closes["W1"][-1])
    closes.pop("W1")   # data feed lost the coin; mid still present
    out = _agent(conn=conn).decide(_view(20 * HOUR, mids, closes))
    flats = [d for d in out if d.action == "flatten"]
    assert len(flats) == 1 and "NO-SIGNAL" in flats[0].reasoning


def test_decide_cooldown_blocks_reentry_after_exit():
    mids, closes = _cross_section()
    conn = init_db(":memory:")
    log_decision(conn, Decision(agent="xmom_v1", action="place", coin="W1",
                                side="B", sz=1.0, px=100.0, reasoning="seed"))
    log_decision(conn, Decision(agent="xmom_v1", action="flatten", coin="W1",
                                side="A", sz=1.0, px=99.0, reasoning="seed"))
    row = conn.execute("SELECT MAX(ts_ms) FROM agent_decisions").fetchone()
    with frozen_clock(row[0] / 1000.0 + 60):   # one minute after the exit
        out = _agent({"reentry_cooldown_hours": 4.0}, conn).decide(
            _view(20 * HOUR, mids, closes))
        assert not any(d.action == "place" and d.coin == "W1" for d in out)
        out = _agent({"reentry_cooldown_hours": 0.0}, conn).decide(
            _view(20 * HOUR, mids, closes))
        assert any(d.action == "place" and d.coin == "W1" for d in out)


# ---------------------------------------------------------------------------
# Engine integration: long the diverging winner, short the loser
# ---------------------------------------------------------------------------


def test_backtest_xmom_profits_on_divergence():
    # 4 coins flat for 15 bars, then WIN trends +0.5%/bar and LOSE −0.5%/bar
    # while F1/F2 stay flat: the book longs WIN, shorts LOSE, and both legs
    # pay net of (maker) costs when liquidated at the end.
    bars = 60
    series = {
        "WIN": [100.0] * 15 + _trend(100.0, 0.005, bars - 15),
        "LOSE": [100.0] * 15 + _trend(100.0, -0.005, bars - 15),
        "F1": [100.0] * bars,
        "F2": [100.0] * bars,
    }
    frames = []
    for i in range(1, bars):
        closes = {c: p[max(0, i + 1 - 12):i + 1] for c, p in series.items()}
        frames.append(Frame(
            ts_ms=i * HOUR, mids={c: p[i] for c, p in series.items()},
            day_ntl_vlm={c: VOL5 for c in series}, closes={c: v for c, v in closes.items()},
        ))
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = XMomAgent(config={"lookback_bars": 10, "top_k": 1, "exit_rank": 2,
                              "min_abs_return": 0.01}, conn=conn)
    res = bt.run(agent, frames)
    assert res.net_pnl > 0
    assert res.scorecard.n_trades >= 2
    sides = {(r["coin"], r["side"]) for r in conn.execute(
        "SELECT coin, side FROM agent_decisions WHERE action='place'")}
    assert ("WIN", "B") in sides and ("LOSE", "A") in sides


def test_backtest_xmom_stays_flat_without_dispersion():
    bars = 40
    series = {c: [100.0] * bars for c in ("A1", "A2", "A3", "A4")}
    frames = []
    for i in range(1, bars):
        closes = {c: p[max(0, i + 1 - 12):i + 1] for c, p in series.items()}
        frames.append(Frame(
            ts_ms=i * HOUR, mids={c: p[i] for c, p in series.items()},
            day_ntl_vlm={c: VOL5 for c in series}, closes=closes,
        ))
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = XMomAgent(config={"lookback_bars": 10, "min_abs_return": 0.01},
                      conn=conn)
    res = bt.run(agent, frames)
    assert res.scorecard.n_trades == 0


def test_backtest_invert_flips_book_on_divergence():
    # Same divergence tape as the momentum integration test: under invert the
    # agent fades it — long the loser, short the winner.
    bars = 60
    series = {
        "WIN": [100.0] * 15 + _trend(100.0, 0.005, bars - 15),
        "LOSE": [100.0] * 15 + _trend(100.0, -0.005, bars - 15),
        "F1": [100.0] * bars,
        "F2": [100.0] * bars,
    }
    frames = []
    for i in range(1, bars):
        closes = {c: p[max(0, i + 1 - 12):i + 1] for c, p in series.items()}
        frames.append(Frame(
            ts_ms=i * HOUR, mids={c: p[i] for c, p in series.items()},
            day_ntl_vlm={c: VOL5 for c in series}, closes=closes,
        ))
    conn = init_db(":memory:")
    bt = Backtester(CostModel(maker=True), conn=conn)
    agent = XMomAgent(config={"invert": True, "lookback_bars": 10, "top_k": 1,
                              "exit_rank": 2, "min_abs_return": 0.01}, conn=conn)
    bt.run(agent, frames)
    sides = {(r["coin"], r["side"]) for r in conn.execute(
        "SELECT coin, side FROM agent_decisions WHERE action='place'")}
    assert ("LOSE", "B") in sides and ("WIN", "A") in sides


def test_backtest_factory_registered():
    from hl_bot.cli.main import _backtest_factories

    factories = _backtest_factories({"lookback_bars": 7, "skip_bars": 3})
    agent = factories["xmom_v1"](init_db(":memory:"))
    assert isinstance(agent, XMomAgent)
    assert agent.cfg.lookback_bars == 7 and agent.cfg.skip_bars == 3
    assert agent.cfg.invert is False   # reversal lever ships default-OFF
    inv = _backtest_factories({"invert": True})["xmom_v1"](init_db(":memory:"))
    assert inv.cfg.invert is True
