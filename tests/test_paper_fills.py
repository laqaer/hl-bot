"""Tests for the paper forward-test fill simulator (scoring/paper_fills.py).

These prove that simulated paper fills reproduce the backtest engine's
price/slip/fee accounting (so a paper agent's G1 forward-test is measured under
the same model that confirmed its edge), and that ``score_paper_forward`` makes
a paper agent — which logs decisions but produces no real fills — measurable.
"""

from __future__ import annotations

from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.backtest.engine import CostModel
from hl_bot.db.schema import init_db
from hl_bot.scoring.paper_fills import score_paper_forward, simulate_paper_fills

MAKER = CostModel(maker=True)     # fee 1bp, slip 0
TAKER = CostModel()               # fee 4.5bps, slip 2bps


def _d(action, coin, side, sz, px, ts):
    return {"ts_ms": ts, "action": action, "coin": coin, "side": side, "sz": sz, "px": px}


def test_long_round_trip_maker_books_price_pnl_and_two_fills():
    fills = simulate_paper_fills(
        [_d("place", "BTC", "B", 1.0, 100.0, 1), _d("flatten", "BTC", "A", 1.0, 110.0, 2)],
        MAKER, agent="t",
    )
    assert len(fills) == 2
    # entry: no slip (maker), fee = 100*1*1e-4, no realized pnl, counts as a trade
    assert fills[0]["closed_pnl"] == 0.0
    assert fills[0]["px"] == 100.0
    assert fills[0]["fee"] == 100.0 * 1e-4
    # exit: price_pnl = (110-100)*1 = +10
    assert fills[1]["closed_pnl"] == 10.0
    assert fills[1]["fee"] == 110.0 * 1e-4


def test_short_round_trip_profits_when_price_falls():
    fills = simulate_paper_fills(
        [_d("place", "ETH", "A", 1.0, 100.0, 1), _d("flatten", "ETH", "B", 1.0, 90.0, 2)],
        MAKER, agent="t",
    )
    assert fills[1]["closed_pnl"] == 10.0  # short 100 -> 90 = +10


def test_taker_costs_strictly_worse_than_maker():
    seq = [_d("place", "BTC", "B", 1.0, 100.0, 1), _d("flatten", "BTC", "A", 1.0, 110.0, 2)]
    m = simulate_paper_fills(seq, MAKER, agent="t")
    t = simulate_paper_fills(seq, TAKER, agent="t")
    m_net = sum(f["closed_pnl"] - f["fee"] for f in m)
    t_net = sum(f["closed_pnl"] - f["fee"] for f in t)
    # taker pays slippage on entry+exit and ~4.5x the fee → less net
    assert t_net < m_net
    # entry slipped up, exit slipped down vs the 100/110 mids
    assert t[0]["px"] == 100.0 * (1 + TAKER.slip)
    assert t[1]["px"] == 110.0 * (1 - TAKER.slip)


def test_partial_close_leaves_remainder_open():
    fills = simulate_paper_fills(
        [_d("place", "SOL", "B", 2.0, 100.0, 1), _d("flatten", "SOL", "A", 1.0, 110.0, 2)],
        MAKER, agent="t",
    )
    assert fills[0]["sz"] == 2.0
    assert fills[1]["sz"] == 1.0 and fills[1]["closed_pnl"] == 10.0
    # only half closed → only that half's pnl is realized so far
    assert sum(f["closed_pnl"] for f in fills) == 10.0


def test_opposite_side_flips_without_double_counting():
    # long 1 @100, then a 2-lot short @110 → closes the long (+10) and opens 1 short
    fills = simulate_paper_fills(
        [_d("place", "BTC", "B", 1.0, 100.0, 1), _d("place", "BTC", "A", 2.0, 110.0, 2)],
        MAKER, agent="t",
    )
    assert len(fills) == 3  # open long, close long, open short-remainder
    assert fills[1]["closed_pnl"] == 10.0  # the long was closed at +10
    assert fills[1]["side"] == "A"
    assert fills[2]["closed_pnl"] == 0.0 and fills[2]["sz"] == 1.0  # leftover short


def test_skips_decisions_missing_price_or_size_and_holds():
    fills = simulate_paper_fills(
        [
            _d("hold", "BTC", None, None, 100.0, 1),        # advisory: ignored
            _d("place", "BTC", "B", 1.0, None, 2),          # no px: skipped
            _d("place", "BTC", "B", None, 100.0, 3),        # no sz: skipped
            _d("flatten", "BTC", "A", 1.0, 110.0, 4),       # no open pos: no-op
        ],
        MAKER, agent="t",
    )
    assert fills == []


def test_simulate_matches_engine_accounting_on_a_round_trip():
    """The simulator's pnl/fee must equal what backtest.engine books for the
    same fill, since both consume the same CostModel — this is the faithfulness
    anchor (paper forward-test == backtest of the same decisions)."""
    cost = MAKER
    # Engine math for a long 1 @100 -> exit @110:
    entry_fill_px = 100.0 * (1 + cost.slip)
    entry_fee = entry_fill_px * 1.0 * cost.fee_rate
    exit_px = 110.0 * (1 - cost.slip)
    price_pnl = (exit_px - entry_fill_px) * 1.0
    exit_fee = exit_px * 1.0 * cost.fee_rate
    fills = simulate_paper_fills(
        [_d("place", "BTC", "B", 1.0, 100.0, 1), _d("flatten", "BTC", "A", 1.0, 110.0, 2)],
        cost, agent="t",
    )
    assert fills[0]["px"] == entry_fill_px and fills[0]["fee"] == entry_fee
    assert fills[1]["px"] == exit_px and fills[1]["fee"] == exit_fee
    assert fills[1]["closed_pnl"] == price_pnl


def test_score_paper_forward_makes_a_paper_agent_measurable():
    conn = init_db(":memory:")
    now = 1_900_000_000_000
    # A profitable paper round-trip logged the way the live paper tick logs it.
    log_decision(conn, Decision(agent="trend", action="place", coin="BTC",
                                side="B", sz=1.0, px=100.0, is_paper=True))
    log_decision(conn, Decision(agent="trend", action="flatten", coin="BTC",
                                side="A", sz=1.0, px=110.0, is_paper=True))
    # fix the logged timestamps to a fixed recent instant so window filtering is stable
    conn.execute("UPDATE agent_decisions SET ts_ms = ? WHERE rowid = 1", (now,))
    conn.execute("UPDATE agent_decisions SET ts_ms = ? WHERE rowid = 2", (now + 1000,))

    sc = score_paper_forward(conn, "trend", "all", MAKER)
    assert sc.n_trades == 2
    assert sc.net_pnl > 9.0  # +10 price pnl minus ~2 maker-bp fees
    assert sc.edge_bps is not None and sc.edge_bps > 0
    # the live ground-truth fills table was never written to (read-only)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_score_paper_forward_ignores_non_paper_decisions():
    conn = init_db(":memory:")
    log_decision(conn, Decision(agent="x", action="place", coin="BTC",
                                side="B", sz=1.0, px=100.0, is_paper=False))
    sc = score_paper_forward(conn, "x", "all", MAKER)
    assert sc.n_trades == 0
