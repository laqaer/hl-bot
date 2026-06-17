"""External transfer ingestion and flow-adjusted equity floor (V6)."""

from __future__ import annotations

import time

from hl_bot.db.schema import init_db
from hl_bot.ingest.hyperliquid import ingest_transfers
from hl_bot.ops.kill import equity_floor_breached

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, timeout=None):
        return _Resp(200, self._payload)


def _seed_equity(conn, ts_ms: int, value: float) -> None:
    conn.execute(
        """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
               total_ntl_pos, total_raw_usd, withdrawable, raw_json)
           VALUES(?, ?, 0, 0, 0, 0, '{}')""",
        (ts_ms, value),
    )


def test_ingest_transfers_signs_and_filters(monkeypatch, tmp_path):
    conn = init_db(tmp_path / "t.sqlite")
    payload = [
        {"delta": {"type": "deposit", "usdc": "1000.0"}, "hash": "h1", "time": NOW_MS - 10 * DAY_MS},
        {"delta": {"type": "withdraw", "usdc": "-400.0"}, "hash": "h2", "time": NOW_MS - 5 * DAY_MS},
        {"delta": {"type": "accountClassTransfer", "usdc": "200.0", "toPerp": True}, "hash": "h3", "time": NOW_MS - 3 * DAY_MS},
    ]
    monkeypatch.setattr("hl_bot.ingest.hyperliquid.httpx.Client", lambda: _Client(payload))
    n = ingest_transfers(conn, "0x123", "http://t", lookback_days=35)
    assert n == 2
    rows = conn.execute("SELECT type, amount FROM transfers ORDER BY time_ms").fetchall()
    assert [(r["type"], r["amount"]) for r in rows] == [("deposit", 1000.0), ("withdraw", -400.0)]


def test_equity_floor_withdrawal_does_not_trip_kill(tmp_path):
    """A pure withdrawal must not trip the equity-floor kill."""
    conn = init_db(tmp_path / "t.sqlite")
    _seed_equity(conn, NOW_MS - 10 * DAY_MS, 1000.0)   # HWM
    _seed_equity(conn, NOW_MS, 600.0)                   # after 400 withdrawal
    conn.execute(
        "INSERT INTO transfers(time_ms, hash, type, amount, raw_json) VALUES(?,?,?,?,?)",
        (NOW_MS - 1, "h", "withdraw", -400.0, "{}"),
    )
    breached, why = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is False, why
    assert "flow-adj HWM" in why


def test_equity_floor_deposit_does_not_mask_drawdown(tmp_path):
    """A deposit inflates the raw HWM; the adjusted floor must still catch a real
    trading drawdown that the unadjusted floor would miss."""
    conn = init_db(tmp_path / "t.sqlite")
    _seed_equity(conn, NOW_MS - 10 * DAY_MS, 1000.0)   # pre-deposit HWM
    _seed_equity(conn, NOW_MS - 5 * DAY_MS, 1400.0)    # peak after 400 deposit
    _seed_equity(conn, NOW_MS, 1100.0)                  # 300 trading loss from peak
    conn.execute(
        "INSERT INTO transfers(time_ms, hash, type, amount, raw_json) VALUES(?,?,?,?,?)",
        (NOW_MS - 6 * DAY_MS, "h", "deposit", 400.0, "{}"),
    )
    breached, why = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is True, why
    # Raw floor would be 0.75 * 1400 = 1050; current 1100 would NOT breach.
    assert "flow-adj HWM" in why


def test_equity_floor_no_transfers_unchanged(tmp_path):
    """Without transfers the adjusted path matches the original unadjusted path."""
    conn = init_db(tmp_path / "t.sqlite")
    _seed_equity(conn, NOW_MS - 10 * DAY_MS, 1000.0)
    _seed_equity(conn, NOW_MS - 1000, 800.0)
    breached, _ = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is False

    _seed_equity(conn, NOW_MS, 700.0)
    breached, why = equity_floor_breached(conn, frac=0.75, now_ms=NOW_MS)
    assert breached is True
    assert "flow-adj HWM" in why
