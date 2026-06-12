"""scripts/auto_tuner.py — M4: auto-apply is risk-tightening only.

The tuner script is standalone (stdlib-only, runs off-box via Hermes cron), so
these tests load it by file path with the tuner's file targets pointed at a
tmp dir via the HLBOT_TUNER_* env overrides.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "auto_tuner.py"


def load_tuner(monkeypatch, tmp_path):
    monkeypatch.setenv("HLBOT_TUNER_OVERRIDES", str(tmp_path / "agent_overrides.json"))
    monkeypatch.setenv(
        "HLBOT_TUNER_PROPOSED", str(tmp_path / "agent_overrides.tuner_proposed.json"))
    monkeypatch.setenv("HLBOT_TUNER_LOG", str(tmp_path / "auto_tuner_log.jsonl"))
    monkeypatch.delenv("HLBOT_TUNER_APPLY_LOOSENING", raising=False)
    spec = importlib.util.spec_from_file_location("auto_tuner_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classify_changes_directions(monkeypatch, tmp_path):
    tuner = load_tuner(monkeypatch, tmp_path)
    current = {
        "sigma_enter": 2.0,
        "max_notional_per_trade": 100.0,
        "stop_loss_pct": 0.015,
        "take_profit_pct": 0.008,
        "max_hold_hours": 4.0,
    }
    approved = {
        "sigma_enter": 2.5,                 # higher entry bar → tighter
        "max_notional_per_trade": 150.0,    # more notional → looser
        "stop_loss_pct": 0.01,              # smaller max loss → tighter
        "take_profit_pct": 0.012,           # no risk direction → never auto-applied
        "max_hold_hours": 4.0,              # no-op → dropped from both
    }
    tightening, loosening = tuner.classify_changes(current, approved)
    assert tightening == {"sigma_enter": 2.5, "stop_loss_pct": 0.01}
    assert loosening == {"max_notional_per_trade": 150.0, "take_profit_pct": 0.012}

    # Looser entry bar goes to the loosening bucket; a key with no current
    # value cannot be proven tighter, so it is proposed, not applied.
    tightening, loosening = tuner.classify_changes(
        {"sigma_enter": 2.0}, {"sigma_enter": 1.5, "min_daily_volume_usd": 5e6})
    assert tightening == {}
    assert loosening == {"sigma_enter": 1.5, "min_daily_volume_usd": 5e6}


def test_dispatch_applies_tightening_and_proposes_loosening(monkeypatch, tmp_path):
    tuner = load_tuner(monkeypatch, tmp_path)
    changes = {"twap_mr_v1": {"sigma_enter": 2.5, "max_notional_per_trade": 180.0}}
    current = {"twap_mr_v1": {"sigma_enter": 2.0, "max_notional_per_trade": 150.0}}

    applied, proposed = tuner.dispatch_changes(changes, current)

    assert applied == {"twap_mr_v1": {"sigma_enter": 2.5}}
    assert proposed == {"twap_mr_v1": {"max_notional_per_trade": 180.0}}
    live = json.loads(tuner.OVERRIDES.read_text())
    assert live == {"twap_mr_v1": {"sigma_enter": 2.5}}  # notional bump NOT live
    doc = json.loads(tuner.PROPOSED.read_text())
    assert doc["overrides"] == {"twap_mr_v1": {"max_notional_per_trade": 180.0}}
    assert "not applied" in doc["note"]


def test_dispatch_all_tightening_writes_no_proposal_file(monkeypatch, tmp_path):
    tuner = load_tuner(monkeypatch, tmp_path)
    applied, proposed = tuner.dispatch_changes(
        {"femr_v1": {"funding_enter_per_hr": 0.0002}},
        {"femr_v1": {"funding_enter_per_hr": 0.00015}},
    )
    assert applied == {"femr_v1": {"funding_enter_per_hr": 0.0002}}
    assert proposed == {}
    assert not tuner.PROPOSED.exists()


def test_dispatch_loosening_env_flag_restores_old_behavior(monkeypatch, tmp_path):
    tuner = load_tuner(monkeypatch, tmp_path)
    monkeypatch.setenv("HLBOT_TUNER_APPLY_LOOSENING", "1")
    changes = {"twap_mr_v1": {"sigma_enter": 1.5, "max_notional_per_trade": 180.0}}
    current = {"twap_mr_v1": {"sigma_enter": 2.0, "max_notional_per_trade": 150.0}}

    applied, proposed = tuner.dispatch_changes(changes, current)

    assert applied == changes
    assert proposed == {}
    assert json.loads(tuner.OVERRIDES.read_text()) == changes
    assert not tuner.PROPOSED.exists()


def test_validate_rails_unchanged_by_m4(monkeypatch, tmp_path):
    """The M4 gate lives in dispatch_changes; validate_proposal's pre-existing
    rails (bounds, TWAP-only scale approval, % change limit) still hold."""
    tuner = load_tuner(monkeypatch, tmp_path)
    summary = {"n_trades": 100}

    # Notional increases on non-TWAP agents are rejected at validation:
    # femr's bump breaches its approved cap; liq_cascade's stays under cap
    # but increases are TWAP-only.
    approved, rejections = tuner.validate_proposal(
        "femr_v1", {"max_notional_per_trade": 20.0},
        {"max_notional_per_trade": 25.0}, summary)
    assert approved == {}
    assert any("approved cap" in r for r in rejections)
    approved, rejections = tuner.validate_proposal(
        "liq_cascade_v1", {"max_notional_per_trade": 20.0},
        {"max_notional_per_trade": 24.0}, summary)
    assert approved == {}
    assert any("TWAP-only" in r for r in rejections)

    # A within-cap TWAP notional increase still VALIDATES (the standing
    # approval) — M4 then routes it to the proposal file, not the live book.
    approved, rejections = tuner.validate_proposal(
        "twap_mr_v1", {"max_notional_per_trade": 150.0},
        {"max_notional_per_trade": 180.0}, summary)
    assert approved == {"max_notional_per_trade": 180.0}
    _, loosening = tuner.classify_changes({"max_notional_per_trade": 150.0}, approved)
    assert loosening == {"max_notional_per_trade": 180.0}

    # >50% swing and out-of-bounds values are still rejected.
    approved, rejections = tuner.validate_proposal(
        "twap_mr_v1", {"sigma_enter": 2.0}, {"sigma_enter": 3.5}, summary)
    assert approved == {} and any("limit" in r for r in rejections)
    approved, rejections = tuner.validate_proposal(
        "twap_mr_v1", {"stop_loss_pct": 0.015}, {"stop_loss_pct": 0.001}, summary)
    assert approved == {} and any("out of bounds" in r for r in rejections)
