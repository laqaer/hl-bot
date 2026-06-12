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


def _paper(conn, agent, t_ms, action, coin, side=None, sz=None, px=None):
    conn.execute(
        """INSERT INTO agent_decisions(ts_ms, agent, action, coin, side, sz, px, is_paper)
           VALUES(?,?,?,?,?,?,?,1)""",
        (t_ms, agent, action, coin, side, sz, px),
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
    jp, mp, hp = export(conn, tmp_path / "tr")
    assert jp.exists() and mp.exists() and hp.exists()
    assert "track record" in mp.read_text().lower()


def test_paper_section_separate_from_live(conn):
    """Paper book gets its own labeled section; live table stays fills-only."""
    from hl_bot.reports.track_record import to_html

    now = int(time.time() * 1000)
    _fill(conn, "twap_mr_v1", now, pnl=5.0)
    # Paper round trip B 1.0 @100 -> 110 plus a still-open ETH entry.
    _paper(conn, "breakout_v1", now - 5000, "place", "BTC", "B", 1.0, 100.0)
    _paper(conn, "breakout_v1", now - 4000, "flatten", "BTC", px=110.0)
    _paper(conn, "breakout_v1", now - 1000, "place", "ETH", "B", 1.0, 200.0)

    tr = build_track_record(conn)
    assert "paper_note" in tr
    assert [a["agent"] for a in tr["agents"]] == ["twap_mr_v1"]
    pg = tr["paper_agents"]
    assert [a["agent"] for a in pg] == ["breakout_v1"]
    ag = pg[0]
    assert ag["n_trades"] == 3                       # entry+exit+open entry legs
    # Modeled taker costs (4.5bps fee, 2bps slip): eff 100.02 -> 109.978,
    # fees 0.045009 + 0.0494901 + (200.04 * 0.00045 open entry).
    assert ag["net_pnl"] == pytest.approx(9.7735, abs=1e-3)
    assert ag["funding_pnl"] == 0.0                  # offline: no rates given
    assert ag["open_positions"] == 1
    assert ag["win_rate"] == pytest.approx(1.0)
    assert ag["windows"]["24h"]["n_trades"] == 3

    md = to_markdown(tr)
    assert "Paper agents (NOT live)" in md
    assert "breakout_v1" in md
    html = to_html(tr)
    assert "Paper agents (NOT live)" in html
    assert "forward test" in html


def test_agent_with_both_books_shows_in_both_tables(conn):
    """Live fills + a paper book (e.g. an A/B paper arm) -> a row in each."""
    now = int(time.time() * 1000)
    _fill(conn, "femr_v1", now, pnl=2.0)
    _paper(conn, "femr_v1", now - 2000, "place", "BTC", "B", 1.0, 100.0)
    _paper(conn, "femr_v1", now - 1000, "flatten", "BTC", px=101.0)

    tr = build_track_record(conn)
    assert [a["agent"] for a in tr["agents"]] == ["femr_v1"]
    assert [a["agent"] for a in tr["paper_agents"]] == ["femr_v1"]
    assert tr["agents"][0]["n_trades"] == 1          # the fill only
    assert tr["paper_agents"][0]["n_trades"] == 2    # the paper legs only


def test_no_paper_book_no_paper_section(conn):
    from hl_bot.reports.track_record import to_html

    _fill(conn, "twap_mr_v1", int(time.time() * 1000), pnl=1.0)
    tr = build_track_record(conn)
    assert "paper_agents" not in tr and "paper_note" not in tr
    assert "Paper agents" not in to_markdown(tr)
    assert "Paper agents" not in to_html(tr)


def test_paper_funding_threads_into_section(conn):
    now = int(time.time() * 1000)
    hour = 3_600_000
    _paper(conn, "femr_v1", now - 5 * hour, "place", "BTC", "B", 1.0, 100.0)
    _paper(conn, "femr_v1", now - 1 * hour, "flatten", "BTC", px=100.0)
    rates = {"BTC": [{"time": now - 3 * hour, "fundingRate": "0.0001"}]}

    tr = build_track_record(conn, paper_funding_by_coin=rates)
    ag = tr["paper_agents"][0]
    # Long pays: -(+1.0) * 100 * 1e-4 = -0.01, folded into net.
    assert ag["funding_pnl"] == pytest.approx(-0.01)
    no_fund = build_track_record(conn)["paper_agents"][0]
    assert ag["net_pnl"] == pytest.approx(no_fund["net_pnl"] - 0.01)


def test_html_export_has_chart_and_stats(conn, tmp_path):
    from hl_bot.reports.track_record import build_track_record, to_html
    now = int(time.time() * 1000)
    day = 86_400_000
    for i in range(4):  # a rising equity curve -> SVG polyline
        conn.execute(
            """INSERT INTO equity_snapshots(ts_ms, account_value, total_margin,
               total_ntl_pos, total_raw_usd, withdrawable, cross_leverage, raw_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (now - (3 - i) * day, 1000.0 + i * 50.0, 0, 0, 1000.0, 1000.0, None, "{}"),
        )
    _fill(conn, "twap_mr_v1", now, pnl=12.0)
    html = to_html(build_track_record(conn))
    assert html.startswith("<!doctype html>")
    assert "<svg" in html and "polyline" in html      # the equity chart rendered
    assert "twap_mr_v1" in html                         # per-agent row present
