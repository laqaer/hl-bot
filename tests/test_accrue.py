from hl_bot.agents.base import MarketView
from hl_bot.db.accrue import accrue_market_snapshot, load_forward_frames
from hl_bot.db.schema import init_db


def test_accrue_market_snapshot_writes_rows():
    conn = init_db(":memory:")
    view = MarketView(
        ts_ms=1_000_000,
        mids={"BTC": 100.0, "ETH": 50.0},
        funding={"BTC": 0.0001},
        open_interest={"ETH": 1_000_000.0},
        book_top={"BTC": (99.5, 100.5)},
        extra={"day_ntl_vlm": {"BTC": 10_000_000.0}},
    )
    n = accrue_market_snapshot(conn, view)
    assert n == 2
    rows = conn.execute("SELECT * FROM market_snapshots ORDER BY coin").fetchall()
    assert len(rows) == 2
    btc = next(r for r in rows if r["coin"] == "BTC")
    assert btc["mid"] == 100.0
    assert btc["funding_1h"] == 0.0001
    assert btc["book_bid"] == 99.5
    assert btc["book_ask"] == 100.5


def test_load_forward_frames_reconstructs_frames():
    conn = init_db(":memory:")
    for ts, mid in [(1_000_000, 100.0), (1_001_000, 101.0)]:
        view = MarketView(
            ts_ms=ts,
            mids={"BTC": mid},
            funding={"BTC": 0.0},
            extra={},
        )
        accrue_market_snapshot(conn, view)
    frames = load_forward_frames(conn)
    assert len(frames) == 2
    assert frames[0].ts_ms == 1_000_000
    assert frames[0].mids["BTC"] == 100.0
    assert frames[1].mids["BTC"] == 101.0


def test_accrue_is_idempotent():
    conn = init_db(":memory:")
    view = MarketView(ts_ms=1, mids={"BTC": 100.0}, extra={})
    accrue_market_snapshot(conn, view)
    accrue_market_snapshot(conn, view)
    rows = conn.execute("SELECT count(*) AS c FROM market_snapshots").fetchone()
    assert rows["c"] == 1
