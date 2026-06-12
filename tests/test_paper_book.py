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
from hl_bot.agents.runtime import classify_position_ownership
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
