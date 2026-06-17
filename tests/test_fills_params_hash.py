from hl_bot.agents.cloid import make_cloid
from hl_bot.agents.decisions import Decision, log_decision
from hl_bot.db.schema import init_db
from hl_bot.ingest.hyperliquid import _params_hash_for_fill, ingest_fills


def test_params_hash_resolved_from_agent_decisions():
    conn = init_db(":memory:")
    d = Decision(
        agent="femr_v1", action="place", coin="BTC", side="B",
        sz=1.0, px=100.0, cloid="0xfemr1234567890ab",
        params_hash="deadbeef12345678",
    )
    log_decision(conn, d)
    assert _params_hash_for_fill(conn, "0xfemr1234567890ab", "femr_v1") == "deadbeef12345678"


def test_params_hash_fallback_to_agent_state():
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO agent_state(agent, mode, confirmed_params_hash) VALUES(?,?,?)",
        ("twap_mr_v1", "live_small", "cafebabe87654321"),
    )
    assert _params_hash_for_fill(conn, None, "twap_mr_v1") == "cafebabe87654321"


def test_ingest_fills_writes_params_hash(monkeypatch):
    conn = init_db(":memory:")
    conn.execute(
        "INSERT INTO agent_state(agent, mode, confirmed_params_hash) VALUES(?,?,?)",
        ("femr_v1", "live_small", "abc123def4567890"),
    )

    fake_fills = [{
        "hash": "0x123", "tid": 1, "time": 1_000_000,
        "coin": "BTC", "side": "B", "px": "100.0", "sz": "1.0",
        "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0", "fee": "0.1", "feeToken": "USDC",
        "builderFee": "0.0",
        "cloid": make_cloid("femr_v1"),
    }]

    def fake_post(_self, _base_url, payload):
        return fake_fills

    monkeypatch.setattr("hl_bot.ingest.hyperliquid._post", fake_post)
    ingest_fills(conn, "0xaddr", "https://api.hyperliquid.xyz")
    row = conn.execute("SELECT params_hash FROM fills WHERE hash=?", ("0x123",)).fetchone()
    assert row["params_hash"] == "abc123def4567890"
