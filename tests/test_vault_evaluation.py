"""Vault-evaluation spike tests — the gate is honest and the record consistent."""

from __future__ import annotations

import json

from hl_bot.reports.vault_evaluation import (
    ASPECTS,
    build_vault_evaluation,
    export,
    to_markdown,
    vault_ready,
)


def test_gate_is_not_ready_until_g3_clears():
    # The load-bearing conclusion: no edge (G3 unmet) -> vault must not open.
    blocked = vault_ready(g3_cleared=False)
    assert blocked["ready"] is False
    assert "G3" in blocked["gate"]
    assert "no" in blocked["reason"].lower()

    ok = vault_ready(g3_cleared=True)
    assert ok["ready"] is True


def test_build_defaults_to_not_ready():
    rec = build_vault_evaluation()
    assert rec["gate"]["ready"] is False


def test_protocol_mechanics_are_flagged_unverified():
    # Integrity: external HL facts must not be laundered into the artifact as truth.
    # Every aspect here is sourced from HL docs, so all are unverified by design.
    assert ASPECTS  # non-empty
    assert all(not a.verified for a in ASPECTS)
    rec = build_vault_evaluation()
    assert rec["n_unverified"] == rec["n_aspects"] == len(ASPECTS)


def test_every_aspect_is_populated():
    for a in ASPECTS:
        assert a.aspect.strip()
        assert a.understanding.strip()
        assert a.why_it_matters.strip()


def test_markdown_renders_gate_and_every_aspect():
    rec = build_vault_evaluation()
    md = to_markdown(rec)
    assert md.startswith("# hl-bot — Hyperliquid vault evaluation (B16)")
    assert "NOT READY" in md
    for a in ASPECTS:
        assert a.aspect in md


def test_markdown_shows_ready_when_g3_cleared():
    md = to_markdown(build_vault_evaluation(g3_cleared=True))
    assert "## Gate: READY" in md


def test_export_writes_valid_json_and_markdown(tmp_path):
    jp, mp = export(tmp_path)
    assert jp.exists() and mp.exists()
    loaded = json.loads(jp.read_text())
    assert loaded["gate"]["ready"] is False
    assert len(loaded["aspects"]) == len(ASPECTS)
    assert mp.read_text().startswith("# hl-bot — Hyperliquid vault evaluation (B16)")
