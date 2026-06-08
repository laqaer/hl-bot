"""Track-record export tests — built from the same tables used live."""

from __future__ import annotations

import time

import pytest

from hl_bot.db.schema import init_db
from hl_bot.reports.track_record import build_track_record, export, to_markdown


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "tr.sqlite")


def _fill(conn, agent, t_ms, pnl, fee=0.1, sz=1.0, px=100.0, coin="BTC"):
    conn.execute(
        """INSERT INTO fills(hash, tid, time_ms, coin, side, px, sz,
           start_position, dir, closed_pnl, fee, fee_token, builder_fee,
           cloid, agent, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"h{agent}{t_ms}", t_ms, t_ms, coin, "B", px, sz, 0, "Close Long",
         pnl, fee, "USDC", 0, None, agent, "{}"),
    )


def _equity(conn, t_ms, val):
    conn.execute(
        """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
           total_ntl_pos, total_raw_usd, withdrawable, cross_leverage, raw_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (t_ms, val, 0, 0, val, val, None, "{}"),
    )


def test_track_record_structure_and_numbers(conn):
    now = int(time.time() * 1000)
    day = 86_400_000
    # 5 daily-spaced winning fills (varying, so daily Sharpe is defined)
    for i, pnl in enumerate([8.0, 12.0, 10.0, 9.0, 11.0]):
        _fill(conn, "twap_mr_regime_v1", now - i * day, pnl=pnl)
    # account equity rising
    for i in range(5):
        _equity(conn, now - i * day, 1000.0 + (4 - i) * 50.0)

    tr = build_track_record(conn)
    assert tr["account"]["start_value"] == pytest.approx(1000.0)
    assert tr["account"]["end_value"] == pytest.approx(1200.0)
    assert tr["account"]["total_return_pct"] == pytest.approx(0.20)

    ag = next(a for a in tr["agents"] if a["agent"] == "twap_mr_regime_v1")
    assert ag["n_trades"] == 5
    assert ag["net_pnl"] == pytest.approx(49.5)          # 5*10 - 5*0.1
    assert ag["sharpe_daily"] is not None
    assert ag["max_drawdown_usd"] is not None and ag["max_drawdown_usd"] <= 0

    md = to_markdown(tr)
    assert "twap_mr_regime_v1" in md
    assert "Account" in md


def test_export_writes_files(conn, tmp_path):
    _fill(conn, "femr_v1", int(time.time() * 1000), pnl=1.0)
    jp, mp = export(conn, tmp_path / "tr")
    assert jp.exists() and mp.exists()
    assert "track record" in mp.read_text().lower()
