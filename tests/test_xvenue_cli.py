"""CLI + wiring for cross-venue funding accrual host job (S5)."""

from __future__ import annotations

from typer.testing import CliRunner

from hl_bot.cli.main import app
from hl_bot.db.schema import init_db

runner = CliRunner()


def test_accrue_xvenue_command(monkeypatch, tmp_path):
    calls = []

    def fake_fetch(coins):
        calls.append(coins)
        return {"BTC": {"binance": 0.0001}, "ETH": {"bybit": 0.0002}}

    monkeypatch.setattr("hl_bot.cli.main.fetch_xvenue_funding", fake_fetch)

    db_path = tmp_path / "x.sqlite"
    monkeypatch.setenv("HLBOT_DB", str(db_path))
    result = runner.invoke(app, ["accrue-xvenue", "--coins", "BTC,ETH"])
    assert result.exit_code == 0, result.output
    assert "2 rows across 2 coins" in result.output
    assert calls == [["BTC", "ETH"]]

    conn = init_db(db_path)
    rows = conn.execute("SELECT coin, venue FROM xvenue_funding ORDER BY coin, venue").fetchall()
    assert [(r["coin"], r["venue"]) for r in rows] == [("BTC", "binance"), ("ETH", "bybit")]


def test_accrue_xvenue_reads_env_universe(monkeypatch, tmp_path):
    calls = []

    def fake_fetch(coins):
        calls.append(coins)
        return {}

    monkeypatch.setattr("hl_bot.cli.main.fetch_xvenue_funding", fake_fetch)
    monkeypatch.setenv("HLBOT_DB", str(tmp_path / "y.sqlite"))
    monkeypatch.setenv("HLBOT_XVENUE_COINS", "SOL,HYPE")
    result = runner.invoke(app, ["accrue-xvenue"])
    assert result.exit_code == 0, result.output
    assert calls == [["SOL", "HYPE"]]
