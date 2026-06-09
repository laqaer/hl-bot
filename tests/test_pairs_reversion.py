"""Confirmation tests for the pairs-reversion (relative-value stat-arb) strategy.

The agent trades the log-ratio spread of a coin pair against its rolling mean:
when the spread z-score is extreme it SHORTs the rich leg and LONGs the cheap leg
(dollar-neutral), holding until the spread reverts. We check:
  * a stretched-up spread → short the rich leg / long the cheap leg, equal dollars;
  * a stretched-down spread → the mirror;
  * a spread sitting at its mean is not traded;
  * too-short history holds;
  * an open pair flattens BOTH legs once the spread reverts inside the exit band;
  * ``_parse_pairs`` accepts the ``'A/B|C/D'`` CLI string form;
  * a backtest books positive PnL when a stretched spread reverts, maker.
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.pairs_reversion import PairsReversionAgent, _parse_pairs
from hl_bot.backtest.engine import Backtester, CostModel, Frame
from hl_bot.db.schema import init_db

HOUR = 3_600_000
LB = 10


def _ab_series(final_a_mult: float, n: int = 12) -> dict[str, list[float]]:
    """B flat at 100; A wiggles ±0.05% around 100 for the baseline, then its final
    bar is ``100 * final_a_mult`` — so the spread's trailing mean≈0 with a small
    nonzero std, and the current z is driven by the final A move."""
    b = [100.0] * n
    a = [100.0 * (1.0005 if i % 2 == 0 else 0.9995) for i in range(n - 1)]
    a.append(100.0 * final_a_mult)
    return {"A": a, "B": b}


def _view(closes: dict[str, list[float]], vol: float = 5e7) -> MarketView:
    mids = {c: s[-1] for c, s in closes.items()}
    return MarketView(
        ts_ms=0, mids=mids,
        extra={"closes": closes, "day_ntl_vlm": {c: vol for c in closes}},
    )


def _agent(conn=None, **cfg):
    base = {"pairs": [("A", "B")], "lookback_bars": LB, "entry_z": 2.0, "exit_z": 0.5}
    base.update(cfg)
    return PairsReversionAgent(config=base, conn=conn or init_db(":memory:"))


def test_stretched_up_shorts_rich_longs_cheap():
    agent = _agent()
    decs = {d.coin: d for d in agent.decide(_view(_ab_series(1.01))) if d.action == "place"}
    assert decs["A"].side == "A"   # A rich → short
    assert decs["B"].side == "B"   # B cheap → long
    # dollar-neutral: each leg ≈ equal notional
    na = decs["A"].sz * decs["A"].px
    nb = decs["B"].sz * decs["B"].px
    assert abs(na - nb) < 0.05 * na


def test_stretched_down_mirrors():
    agent = _agent()
    decs = {d.coin: d for d in agent.decide(_view(_ab_series(0.99))) if d.action == "place"}
    assert decs["A"].side == "B"   # A cheap → long
    assert decs["B"].side == "A"   # B rich → short


def test_spread_at_mean_not_traded():
    agent = _agent()
    assert {d.action for d in agent.decide(_view(_ab_series(1.0)))} == {"hold"}


def test_short_history_holds():
    agent = _agent()
    closes = {"A": [100.0, 101.0, 100.5], "B": [100.0, 100.0, 100.0]}
    assert all(d.action == "hold" for d in agent.decide(_view(closes)))


def test_open_pair_flattens_both_legs_on_reversion():
    conn = init_db(":memory:")
    # seed an open short-A / long-B pair from an earlier (stretched) tick
    for coin, side in (("A", "A"), ("B", "B")):
        log_decision(conn, Decision(
            agent="pairs_reversion_v1", action="place", coin=coin, side=side,
            sz=0.25, px=100.0, cloid="x"))
    agent = _agent(conn=conn)
    # spread now back at its mean → both legs should flatten
    flats = {d.coin: d for d in agent.decide(_view(_ab_series(1.0))) if d.action == "flatten"}
    assert set(flats) == {"A", "B"}


def test_parse_pairs_string_form():
    assert _parse_pairs("ETH/BTC|SOL/AVAX") == [("ETH", "BTC"), ("SOL", "AVAX")]
    assert _parse_pairs("eth/btc") == [("ETH", "BTC")]          # upper-cased
    assert _parse_pairs("ETH/ETH|bad|X/Y") == [("X", "Y")]       # self-pair & malformed dropped


def test_backtest_books_positive_pnl_on_reverting_spread():
    conn = init_db(":memory:")
    # A dislocates up vs flat B for a single bar (bar 25 → 108), then snaps back to
    # par and stays: the agent shorts the rich A at the extreme and covers at par
    # the next bar → profit on the A leg; B leg ≈ flat.
    def a_price(i: int) -> float:
        if i == 25:
            return 108.0                                       # one-bar dislocation
        return 100.0 * (1.0005 if i % 2 == 0 else 0.9995)      # par ± tiny noise

    frames = []
    a_path: list[float] = []
    for i in range(40):
        a_path.append(a_price(i))
        win_a = a_path[max(0, i - 29):i + 1]
        win_b = [100.0] * len(win_a)
        frames.append(Frame(
            ts_ms=i * HOUR,
            mids={"A": a_path[-1], "B": 100.0},
            day_ntl_vlm={"A": 5e7, "B": 5e7},
            closes={"A": win_a, "B": win_b},
        ))
    bt = Backtester(CostModel(maker=True), conn=conn)
    res = bt.run(_agent(conn=conn), frames)
    traded = {r[0] for r in conn.execute("SELECT DISTINCT coin FROM fills").fetchall()}
    assert {"A", "B"} <= traded
    assert res.net_pnl > 0
