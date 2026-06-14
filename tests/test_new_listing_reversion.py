"""Evidence test for new_listing_reversion_v1 (D2b).

The thesis: a fresh perp listing overshoots its listing price on day 1, then
mean-reverts. The agent fades a >=min_runup gap from the listing reference while
the coin is still within day 1, sized as a hard-capped moonshot sleeve, with a
wide stop for the violent "keeps mooning" tail. These tests pin entry direction,
the age / runup / volume gates, each exit reason, and the live-gap (no
new_listings → hold).
"""

from __future__ import annotations

from hl_bot.agents.base import MarketView
from hl_bot.agents.new_listing_reversion import NewListingReversionAgent
from hl_bot.db.schema import init_db

COIN = "NEWQ"
VOL = 5_000_000.0


def _view(mid, ref, age_bars, vol_usd=VOL, ts_ms=0):
    return MarketView(
        ts_ms=ts_ms,
        mids={COIN: mid},
        extra={"new_listings": {COIN: {
            "age_bars": age_bars, "ref_px": ref,
            "vol_usd": vol_usd, "recent_closes": [ref, mid],
        }}},
    )


def _agent(config=None):
    conn = init_db(":memory:")
    return NewListingReversionAgent(config=config or {}, conn=conn), conn


def _places(decisions):
    return [d for d in decisions if d.action == "place"]


# -- entries: direction & gates -------------------------------------------

def test_day1_pop_goes_short():
    # mid 40% above the listing ref, age 5 bars -> SHORT (fade the pop).
    agent, _ = _agent()
    p = _places(agent.decide(_view(14.0, 10.0, 5)))
    assert len(p) == 1 and p[0].coin == COIN and p[0].side == "A"


def test_day1_dump_goes_long():
    # mid 40% below the listing ref -> LONG (fade the dump).
    agent, _ = _agent()
    p = _places(agent.decide(_view(6.0, 10.0, 5)))
    assert len(p) == 1 and p[0].side == "B"


def test_small_runup_holds():
    agent, _ = _agent()  # only +10% < 25% gate
    assert _places(agent.decide(_view(11.0, 10.0, 5))) == []


def test_past_day1_holds():
    agent, _ = _agent()  # big pop but age 40 > max_age_bars 24
    assert _places(agent.decide(_view(14.0, 10.0, 40))) == []


def test_illiquid_listing_holds():
    agent, _ = _agent()  # big pop, fresh, but below min_listing_vol_usd
    assert _places(agent.decide(_view(14.0, 10.0, 5, vol_usd=100_000.0))) == []


def test_no_new_listings_holds():
    # The live gap: a view without new_listings (live build_view) -> hold only.
    agent, _ = _agent()
    out = agent.decide(MarketView(ts_ms=0, mids={COIN: 14.0}, extra={}))
    assert _places(out) == [] and out[0].action == "hold"


# -- exits -----------------------------------------------------------------

def _seed_open(agent, conn, side, sz, entry_px):
    from hl_bot.agents.decisions import Decision, log_decision
    log_decision(conn, Decision(agent=agent.name, action="place", coin=COIN,
                                side=side, sz=sz, px=entry_px, is_paper=True))


def test_exit_on_revert_to_reference():
    agent, conn = _agent()
    _seed_open(agent, conn, "A", 1.0, 14.0)         # shorted the pop at 14
    out = agent.decide(_view(10.5, 10.0, 9))         # back near ref (runup +5%)
    flat = [d for d in out if d.action == "flatten"]
    assert len(flat) == 1 and "REVERTED" in flat[0].reasoning


def test_exit_on_stop():
    agent, conn = _agent()
    _seed_open(agent, conn, "A", 1.0, 14.0)         # short at 14
    out = agent.decide(_view(16.0, 10.0, 9))         # +14% against us > 8% stop
    flat = [d for d in out if d.action == "flatten"]
    assert len(flat) == 1 and "STOP" in flat[0].reasoning
