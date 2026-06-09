"""Deploy-config consistency for the fine-cadence recorder service.

The recorder (`hlbot record-trades`) is the only route to a months-long 1m/5m
archive (HL retains ~one candle cap), so it must run 24/7 under systemd. These
tests guard against the unit silently drifting out of the deploy wiring.
"""

from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVICE = DEPLOY / "systemd" / "hlbot-recorder.service"


def test_recorder_service_file_exists():
    assert SERVICE.is_file()


def test_recorder_service_runs_record_trades_command():
    text = SERVICE.read_text()
    assert "hlbot record-trades" in text
    # Driven by env so the operator can tune coins/interval/archive without edits.
    for var in ("HLBOT_RECORD_COINS", "HLBOT_RECORD_INTERVAL", "HLBOT_RECORD_ARCHIVE"):
        assert var in text


def test_recorder_service_is_long_running_and_sandboxed():
    text = SERVICE.read_text()
    assert "Type=simple" in text
    assert "Restart=always" in text
    # Writes only under data/ (the archive lives there); rest of FS is read-only.
    assert "ReadWritePaths=/opt/hl-bot/data" in text


def test_install_enables_recorder_service():
    text = (DEPLOY / "install.sh").read_text()
    assert "hlbot-recorder.service" in text


def test_update_restarts_recorder_service():
    text = (DEPLOY / "update.sh").read_text()
    assert "hlbot-recorder.service" in text


def test_env_example_documents_recorder_vars():
    text = (DEPLOY / "env.example").read_text()
    for var in ("HLBOT_RECORD_COINS", "HLBOT_RECORD_INTERVAL", "HLBOT_RECORD_ARCHIVE"):
        assert var in text
