from hl_bot.agents.base import MarketView
from hl_bot.db.schema import init_db
from hl_bot.ingest.universe import detect_new_listings


def fake_fetch_candles(coin, interval, start_ms, end_ms, base_url):
    return [{"t": start_ms, "c": 100.0}]


def test_detect_new_listings_inserts_new_coins():
    conn = init_db(":memory:")
    view = MarketView(
        ts_ms=1_000_000,
        mids={"NEWCOIN": 10.0, "OLDCOIN": 20.0},
        extra={},
    )
    conn.execute(
        "INSERT INTO new_listings(coin, first_seen_ms) VALUES(?,?)",
        ("OLDCOIN", 500_000),
    )
    new = detect_new_listings(conn, view, "https://api.hyperliquid.xyz", fetch_candles_fn=fake_fetch_candles)
    assert new == ["NEWCOIN"]
    row = conn.execute(
        "SELECT * FROM new_listings WHERE coin=?", ("NEWCOIN",)
    ).fetchone()
    assert row is not None
    assert row["first_seen_ms"] == 1_000_000
    assert row["first_listed_px"] == 10.0


def test_detect_new_listings_is_idempotent():
    conn = init_db(":memory:")
    view = MarketView(ts_ms=1, mids={"BTC": 100.0}, extra={})
    detect_new_listings(conn, view, "https://api.hyperliquid.xyz", fetch_candles_fn=fake_fetch_candles)
    detect_new_listings(conn, view, "https://api.hyperliquid.xyz", fetch_candles_fn=fake_fetch_candles)
    rows = conn.execute("SELECT count(*) AS c FROM new_listings").fetchone()
    assert rows["c"] == 1
