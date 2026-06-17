"""Forward-evidence accrual (P1): append-only capture of the signals HL candle
history can't give us, plus the live new_listings wiring.

Pins: market_samples throttling/columns, the listing_log backfill guard (the
existing universe must NOT look day-1 on first run), the live new_listings
signal shape, and xvenue funding APR conversion.
"""

from __future__ import annotations

import pytest

from hl_bot.agents.base import MarketView
from hl_bot.db.schema import init_db
from hl_bot.ingest.accrual import (
    accrue_cycle,
    accrue_listings,
    accrue_market_samples,
    accrue_xvenue_funding,
    build_new_listings_view,
)

HOUR = 3_600_000


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.sqlite")


def _view(mids, *, funding=None, oi=None, vlm=None, imb=None):
    return MarketView(
        ts_ms=0, mids=dict(mids), funding=dict(funding or {}),
        open_interest=dict(oi or {}),
        extra={"day_ntl_vlm": dict(vlm or {}), "book_imb": dict(imb or {})},
    )


# --- migration ------------------------------------------------------------

def test_migration_creates_accrual_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"market_samples", "xvenue_funding", "listing_log"} <= tables


# --- market_samples -------------------------------------------------------

def test_market_samples_writes_perp_rows_with_signals(conn):
    v = _view({"BTC": 64000.0, "ETH": 3200.0, "BTC-SPOT": 64010.0},
              funding={"BTC": 1.25e-5}, oi={"BTC": 99.0}, vlm={"BTC": 1e9},
              imb={"BTC": 0.4})
    n = accrue_market_samples(conn, v, now_ms=10 * HOUR)
    assert n == 2  # BTC + ETH; the -SPOT synthetic mid is skipped
    row = conn.execute(
        "SELECT mid, funding, open_interest, day_ntl_vlm, book_imb "
        "FROM market_samples WHERE coin='BTC'").fetchone()
    assert row["mid"] == 64000.0 and row["funding"] == 1.25e-5
    assert row["open_interest"] == 99.0 and row["book_imb"] == 0.4
    assert conn.execute(
        "SELECT COUNT(*) FROM market_samples WHERE coin='BTC-SPOT'").fetchone()[0] == 0


def test_market_samples_throttles_per_coin(conn):
    v = _view({"BTC": 64000.0})
    assert accrue_market_samples(conn, v, now_ms=10 * HOUR, min_interval_s=60) == 1
    # 30s later: throttled (< min_interval_s)
    assert accrue_market_samples(conn, v, now_ms=10 * HOUR + 30_000, min_interval_s=60) == 0
    # 90s later: writes again
    assert accrue_market_samples(conn, v, now_ms=10 * HOUR + 90_000, min_interval_s=60) == 1
    assert conn.execute("SELECT COUNT(*) FROM market_samples").fetchone()[0] == 2


# --- listing_log (the backfill guard is the important one) ----------------

def test_first_run_backfills_universe_as_known_not_new(conn):
    v = _view({"BTC": 64000.0, "ETH": 3200.0})
    n = accrue_listings(conn, v, now_ms=10 * HOUR)
    assert n == 2
    srcs = {r["coin"]: r["source"] for r in conn.execute(
        "SELECT coin, source FROM listing_log").fetchall()}
    assert srcs == {"BTC": "backfill", "ETH": "backfill"}
    # backfilled coins are NOT new listings even though just first-seen
    nl = build_new_listings_view(conn, v, now_ms=10 * HOUR)
    assert nl == {}


def test_coin_appearing_after_seed_is_a_live_listing(conn):
    accrue_listings(conn, _view({"BTC": 64000.0}), now_ms=10 * HOUR)   # seed
    # NEWCOIN shows up later -> genuine listing, ref px captured
    n = accrue_listings(conn, _view({"BTC": 64000.0, "NEW": 5.0}), now_ms=11 * HOUR)
    assert n == 1
    row = conn.execute("SELECT source, listing_px FROM listing_log WHERE coin='NEW'").fetchone()
    assert row["source"] == "live" and row["listing_px"] == 5.0


def test_build_new_listings_only_live_within_day1(conn):
    accrue_listings(conn, _view({"BTC": 64000.0}), now_ms=0)            # seed
    accrue_listings(conn, _view({"BTC": 64000.0, "NEW": 5.0}), now_ms=HOUR)  # NEW @ ref 5
    # 6h after listing, mid popped to 7.0 (+40%): within day-1, qualifies.
    v = _view({"BTC": 64000.0, "NEW": 7.0}, vlm={"NEW": 2e6})
    nl = build_new_listings_view(conn, v, now_ms=HOUR + 6 * HOUR,
                                 max_age_bars=24, bar_seconds=3600)
    assert set(nl) == {"NEW"}
    info = nl["NEW"]
    assert info["ref_px"] == 5.0 and info["age_bars"] == 6 and info["vol_usd"] == 2e6
    # past day 1 (30h old) -> drops out
    nl2 = build_new_listings_view(conn, v, now_ms=HOUR + 30 * HOUR,
                                  max_age_bars=24, bar_seconds=3600)
    assert nl2 == {}
    # and it wired the signal onto the view
    assert "new_listings" in v.extra


# --- xvenue funding -------------------------------------------------------

def test_xvenue_funding_appends_apr_and_hl_leg(conn):
    # 1.25e-5/hr ~ 0.0000125 * 8760 * 100 = ~10.95% APR
    xv = {"BTC": {"binance": 1.0e-5, "bybit": 1.2e-5}}
    n = accrue_xvenue_funding(conn, xv, hl_funding={"BTC": 1.25e-5}, now_ms=HOUR)
    assert n == 3
    venues = {r["venue"]: r["funding_apr"] for r in conn.execute(
        "SELECT venue, funding_apr FROM xvenue_funding WHERE coin='BTC'").fetchall()}
    assert set(venues) == {"binance", "bybit", "hl"}
    assert venues["hl"] == pytest.approx(1.25e-5 * 24 * 365 * 100, rel=1e-9)


# --- cycle integration ----------------------------------------------------

def test_accrue_cycle_runs_all_legs_and_wires_view(conn):
    accrue_listings(conn, _view({"BTC": 1.0}), now_ms=0)  # seed first
    v = _view({"BTC": 64000.0, "NEW": 5.0}, funding={"BTC": 1e-5}, vlm={"NEW": 3e6})
    out = accrue_cycle(conn, v, now_ms=HOUR)
    assert out["samples"] == 2 and out["listings"] == 1
    # NEW is age 0 (just listed this cycle) -> within day 1 -> wired
    assert v.extra["new_listings"].get("NEW", {}).get("ref_px") == 5.0


def test_accrue_cycle_uses_configured_oi_lookback(conn):
    # Use non-zero timestamps (now_ms=0 is falsy and falls back to _now_ms()).
    # OI=100 at t=30min, 1h; OI=110 at t=1h12min. At t=1h20min with current OI=110:
    #   lookback 5min  -> ref at 1h15min = 110 -> change 0
    #   lookback 25min -> ref at 55min   = 100 -> change +10%
    base = HOUR
    accrue_market_samples(conn, _view({"BTC": 100.0}, oi={"BTC": 100.0}),
                          now_ms=base - 30 * 60 * 1000, min_interval_s=0)
    accrue_market_samples(conn, _view({"BTC": 100.0}, oi={"BTC": 100.0}),
                          now_ms=base, min_interval_s=0)
    accrue_market_samples(conn, _view({"BTC": 100.0}, oi={"BTC": 110.0}),
                          now_ms=base + 12 * 60 * 1000, min_interval_s=0)
    v_short = _view({"BTC": 100.0}, oi={"BTC": 110.0})
    accrue_cycle(conn, v_short, now_ms=base + 20 * 60 * 1000, oi_lookback_s=5 * 60)
    v_long = _view({"BTC": 100.0}, oi={"BTC": 110.0})
    accrue_cycle(conn, v_long, now_ms=base + 20 * 60 * 1000, oi_lookback_s=25 * 60)
    assert v_short.extra["oi_change"].get("BTC") == pytest.approx(0.0)
    assert v_long.extra["oi_change"].get("BTC") == pytest.approx(0.1)
