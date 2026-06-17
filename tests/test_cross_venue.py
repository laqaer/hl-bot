from hl_bot.db.schema import init_db
from hl_bot.ingest import cross_venue


def test_accrue_cross_venue_funding(monkeypatch):
    conn = init_db(":memory:")

    def fake_binance(coin, limit=1):
        return [{"ts_ms": 1_000_000, "funding_1h": 0.0001}]

    def fake_bybit(coin, limit=1):
        return [{"ts_ms": 1_000_000, "funding_1h": 0.0002}]

    monkeypatch.setattr(cross_venue, "fetch_binance_funding", fake_binance)
    monkeypatch.setattr(cross_venue, "fetch_bybit_funding", fake_bybit)

    counts = cross_venue.accrue_cross_venue_funding(conn, ["BTC", "ETH"])
    assert counts == {"binance": 2, "bybit": 2}

    rows = conn.execute("SELECT count(*) AS c FROM funding_cross_venue").fetchone()
    assert rows["c"] == 4

    btc_binance = conn.execute(
        "SELECT funding_1h FROM funding_cross_venue WHERE coin=? AND venue=?",
        ("BTC", "binance"),
    ).fetchone()
    assert btc_binance["funding_1h"] == 0.0001


def test_hl_to_usdt_mapping():
    assert cross_venue._hl_to_usdt("BTC") == "BTCUSDT"
    assert cross_venue._hl_to_usdt("USDC") is None
