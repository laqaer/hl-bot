"""Paper/live decision-book separation (B-PAPER, prereq for B-EDGE2a).

``femr_tick`` in paper mode now logs place/flatten at gather time (is_paper=1)
so a real paper book exists — before, paper ticks logged nothing and paper
agents could never track their own positions. That makes mixed-book DBs
possible, so every replay of ``agent_decisions`` must pick ONE book:

- agents replay the book matching the tick mode (``Agent.paper_book``, set by
  ``gather_decisions``) — a live tick must never flatten a phantom paper
  position, a paper tick must never adopt a live one;
- ``bot_owned_coins`` / ``coin_in_cooldown`` default to the LIVE book, so paper
  rows can never reclassify a manual position as bot-owned (losing the
  don't-touch protection) or gate live entries;
- ``reconcile_positions`` is live-book only: exchange truth says nothing about
  paper positions, so reconciling them would force-flatten the paper book.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.basis import BasisAgent
from hl_bot.agents.breakout import BreakoutAgent
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.agents.femr import FemrAgent
from hl_bot.agents.funding_carry import FundingCarryAgent
from hl_bot.agents.liq_cascade import LiqCascadeAgent
from hl_bot.agents.runtime import classify_position_ownership, synthesize_paper_positions
from hl_bot.agents.twap_mr import TwapMrAgent
from hl_bot.agents.xfund_carry import XFundCarryAgent
from hl_bot.db.schema import init_db
from hl_bot.exec.orders import bot_owned_coins, coin_in_cooldown, reconcile_positions


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _place(conn, agent, coin, paper):
    log_decision(conn, Decision(
        agent=agent, action="place", coin=coin, side="B", sz=1.0, px=100.0,
        is_paper=paper,
    ))


def _open_coins(agent) -> set[str]:
    """The coins an agent's own replay believes it holds."""
    if isinstance(agent, BreakoutAgent):
        return set(agent._position_state()[0])
    if isinstance(agent, FemrAgent):
        return set(agent._femr_open_positions())
    return set(agent._open_positions())


@pytest.mark.parametrize("cls", [
    BreakoutAgent, TwapMrAgent, FemrAgent, LiqCascadeAgent, BasisAgent,
    FundingCarryAgent, XFundCarryAgent,
])
def test_agent_replay_reads_only_its_book(conn, cls):
    # Same agent name, one paper open (BTC) and one live open (ETH) in one DB.
    agent = cls(config={}, conn=conn)
    _place(conn, agent.name, "BTC", paper=True)
    _place(conn, agent.name, "ETH", paper=False)

    agent.paper_book = True
    assert _open_coins(agent) == {"BTC"}, "paper tick replays only the paper book"
    agent.paper_book = False
    assert _open_coins(agent) == {"ETH"}, "live tick replays only the live book"


def test_bot_owned_coins_selects_book(conn):
    _place(conn, "a", "BTC", paper=True)
    _place(conn, "a", "ETH", paper=False)
    assert bot_owned_coins(conn, "a") == {"ETH"}, "default = live book"
    assert bot_owned_coins(conn, "a", paper=True) == {"BTC"}


def test_coin_in_cooldown_ignores_paper_rows(conn):
    # A recent PAPER place must not block a LIVE entry on the same coin.
    _place(conn, "a", "BTC", paper=True)
    assert not coin_in_cooldown(conn, "BTC", agent="a")
    assert coin_in_cooldown(conn, "BTC", agent="a", paper=True)
    _place(conn, "a", "BTC", paper=False)
    assert coin_in_cooldown(conn, "BTC", agent="a")


def test_reconcile_never_flattens_the_paper_book(conn):
    # Paper position with (correctly) no exchange counterpart: reconcile must
    # not write a flatten for it — paper exits belong to the agent's own logic.
    _place(conn, "a", "BTC", paper=True)
    assert reconcile_positions(conn, live_positions=[], agent="a") == []
    assert bot_owned_coins(conn, "a", paper=True) == {"BTC"}, "paper book intact"

    # A stale LIVE position is still reconciled, and the synthetic flatten
    # clears live ownership without touching the paper book.
    _place(conn, "a", "ETH", paper=False)
    assert reconcile_positions(conn, live_positions=[], agent="a") == ["ETH"]
    assert bot_owned_coins(conn, "a") == set()
    assert bot_owned_coins(conn, "a", paper=True) == {"BTC"}


def test_femr_does_not_reenter_coin_it_paper_holds(conn):
    # FEMR's entry dedup keys off exchange positions ("adopt" semantics); a
    # paper position never appears there, so without counting its own replay
    # as active it would re-enter the same coin every paper tick.
    from hl_bot.agents.base import MarketView

    femr = FemrAgent(config={}, conn=conn)
    view = MarketView(
        ts_ms=0,
        mids={"XMR": 100.0},
        funding={"XMR": -0.001},                     # well past the entry threshold
        extra={"day_ntl_vlm": {"XMR": 1e8}, "live_positions": []},
    )
    assert any(d.action == "place" and d.coin == "XMR" for d in femr.decide(view)), \
        "control: with no open position the signal enters"

    _place(conn, femr.name, "XMR", paper=True)
    femr.paper_book = True
    assert not any(d.action == "place" and d.coin == "XMR" for d in femr.decide(view)), \
        "paper-held coin is active: no re-entry"


def test_classify_position_ownership_selects_book(conn):
    # A real (manual) BTC position plus a PAPER BTC row for a roster agent:
    # on the live book BTC stays manual — the don't-touch protection holds.
    _place(conn, "a", "BTC", paper=True)
    live = [{"coin": "BTC"}]
    out = classify_position_ownership(conn, live, ["a"])
    assert out.owned_all == set()
    assert out.manual_coins == ["BTC"]
    # The paper-tick view of the same DB sees the paper book.
    out_paper = classify_position_ownership(conn, live, ["a"], paper=True)
    assert out_paper.owned_all == {"BTC"}
    assert out_paper.manual_coins == []


# ---------------------------------------------------------------------------
# B-PAPER2 — femr paper-exit fidelity: paper ticks synthesize the position
# view femr's exit logic needs from the paper-book replay (a paper position
# has no exchange counterpart, so before this it could never exit).
# ---------------------------------------------------------------------------


def _row(conn, agent, action, coin, side=None, sz=None, px=None, paper=True):
    log_decision(conn, Decision(
        agent=agent, action=action, coin=coin, side=side, sz=sz, px=px,
        is_paper=paper,
    ))


def test_synthesize_paper_positions_long_short(conn):
    _row(conn, "femr_v1", "place", "BTC", side="B", sz=2.0, px=100.0)
    _row(conn, "femr_v1", "place", "ETH", side="A", sz=3.0, px=10.0)
    out = {p["coin"]: p for p in synthesize_paper_positions(
        conn, "femr_v1", {"BTC": 110.0, "ETH": 9.0})}
    btc, eth = out["BTC"], out["ETH"]
    assert btc["szi"] == 2.0 and btc["entry_px"] == 100.0
    assert btc["position_value"] == pytest.approx(220.0)      # marked at mid
    assert btc["unrealized_pnl"] == pytest.approx(20.0)       # long, price up
    assert eth["szi"] == -3.0 and eth["entry_px"] == 10.0
    assert eth["position_value"] == pytest.approx(27.0)
    assert eth["unrealized_pnl"] == pytest.approx(3.0)        # short, price down
    assert btc["liquidation_px"] == 0.0, "0 disables femr's liq-proximity check"


def test_synthesize_flatten_closes_and_replace_overwrites(conn):
    _row(conn, "femr_v1", "place", "BTC", side="B", sz=1.0, px=100.0)
    _row(conn, "femr_v1", "flatten", "BTC", sz=1.0, px=99.0)
    assert synthesize_paper_positions(conn, "femr_v1", {"BTC": 99.0}) == []
    # A re-place on a held coin overwrites, like the agents' own replays.
    _row(conn, "femr_v1", "place", "ETH", side="B", sz=1.0, px=10.0)
    _row(conn, "femr_v1", "place", "ETH", side="A", sz=2.0, px=12.0)
    (eth,) = synthesize_paper_positions(conn, "femr_v1", {"ETH": 12.0})
    assert eth["szi"] == -2.0 and eth["entry_px"] == 12.0


def test_synthesize_skips_unfillable_and_live_rows(conn):
    _row(conn, "femr_v1", "place", "BTC", side=None, sz=1.0, px=100.0)   # no side
    _row(conn, "femr_v1", "place", "SOL", side="B", sz=0.0, px=100.0)    # no size
    _row(conn, "femr_v1", "place", "DOGE", side="B", sz=1.0, px=0.0)     # no px
    _row(conn, "femr_v1", "place", "ETH", side="B", sz=1.0, px=100.0, paper=False)
    assert synthesize_paper_positions(conn, "femr_v1", {"ETH": 100.0}) == []


def test_synthesize_marks_at_entry_when_mid_missing(conn):
    _row(conn, "femr_v1", "place", "BTC", side="B", sz=2.0, px=100.0)
    (btc,) = synthesize_paper_positions(conn, "femr_v1", {})
    assert btc["position_value"] == pytest.approx(200.0)
    assert btc["unrealized_pnl"] == pytest.approx(0.0)


@pytest.mark.parametrize(("mid", "label"), [
    (98.0, "STOP-LOSS"),       # −2% ≤ −1.5% default stop
    (101.0, "TAKE-PROFIT"),    # +1% ≥ +0.8% default TP
])
def test_femr_paper_position_exits(conn, mid, label):
    # End-to-end B-PAPER2: a paper femr position fed back through the
    # synthesized position view hits femr's own exit ladder, and the logged
    # paper flatten closes the book for the next tick.
    from hl_bot.agents.base import MarketView

    femr = FemrAgent(config={}, conn=conn)
    femr.paper_book = True
    _row(conn, femr.name, "place", "XMR", side="B", sz=0.2, px=100.0)
    view = MarketView(
        ts_ms=0, mids={"XMR": mid}, funding={},
        extra={
            "day_ntl_vlm": {"XMR": 1e8},
            "live_positions": synthesize_paper_positions(
                conn, femr.name, {"XMR": mid}),
        },
    )
    flats = [d for d in femr.decide(view) if d.action == "flatten"]
    assert len(flats) == 1 and flats[0].coin == "XMR"
    assert label in (flats[0].reasoning or "")
    assert flats[0].sz == pytest.approx(0.2)

    log_decision(conn, flats[0])  # what gather_decisions does on a paper tick
    assert synthesize_paper_positions(conn, femr.name, {"XMR": mid}) == []
