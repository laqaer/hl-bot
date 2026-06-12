"""Profile isolation (moonshot sleeve): own data dir/DB/KILL/configs, plus the
WS liquidation-event log that builds the cascade dataset."""

from __future__ import annotations

from hl_bot.config import CONFIG_DIR, DATA_DIR, Settings
from hl_bot.ingest.ws import append_liq_events


def test_profile_derives_isolated_paths(monkeypatch):
    monkeypatch.delenv("HLBOT_DB", raising=False)
    monkeypatch.setenv("HLBOT_PROFILE", "moonshot")
    s = Settings.from_env()
    assert s.profile == "moonshot"
    assert s.db_path == DATA_DIR / "moonshot" / "hlbot.sqlite"
    # own data dir => own sticky KILL file, scorecards, equity floor
    assert s.db_path.parent.name == "moonshot"
    # own contract set
    assert s.configs_dir == CONFIG_DIR / "moonshot"


def test_core_profile_unchanged(monkeypatch):
    monkeypatch.delenv("HLBOT_PROFILE", raising=False)
    monkeypatch.delenv("HLBOT_DB", raising=False)
    s = Settings.from_env()
    assert s.profile is None
    assert s.configs_dir == CONFIG_DIR
    assert s.db_path == DATA_DIR / "hlbot.sqlite"


def test_explicit_db_env_wins(monkeypatch):
    monkeypatch.setenv("HLBOT_PROFILE", "moonshot")
    monkeypatch.setenv("HLBOT_DB", "/tmp/custom.sqlite")
    s = Settings.from_env()
    assert str(s.db_path) == "/tmp/custom.sqlite"


def test_append_liq_events_dedupes(tmp_path):
    log = tmp_path / "liq_log.jsonl"
    seen: set = set()
    liqs = [
        {"ts_ms": 1, "coin": "BTC", "sz": 1.0, "px": 100.0, "liquidation": True},
        {"ts_ms": 2, "coin": "ETH", "sz": 2.0, "px": 50.0, "liquidation": True},
    ]
    assert append_liq_events(liqs, log, seen) == 2
    # Same window re-reported next second: nothing new is written.
    assert append_liq_events(liqs, log, seen) == 0
    liqs.append({"ts_ms": 3, "coin": "BTC", "sz": 0.5, "px": 99.0, "liquidation": True})
    assert append_liq_events(liqs, log, seen) == 1
    assert len(log.read_text().splitlines()) == 3
