"""Moonshot-sleeve spec tests — the loss bound holds and the gate stays closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_bot.reports.moonshot_sleeve import (
    CONSTRAINTS,
    SLEEVE_MAX_FRACTION,
    build_moonshot_sleeve,
    export,
    moonshot_gate,
    moonshot_sizing,
    to_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_loss_bound_invariant():
    # The load-bearing fact: worst-case total loss == sleeve budget, and the
    # core floor (capital outside the sleeve) is positive and untouched.
    s = moonshot_sizing(10_000.0, sleeve_fraction=0.05, max_bets=5)
    assert s.sleeve_budget == pytest.approx(500.0)
    assert s.per_bet_max_loss == pytest.approx(100.0)
    assert s.worst_case_total_loss == pytest.approx(s.sleeve_budget)
    assert s.core_floor == pytest.approx(9_500.0)
    assert s.core_floor > 0  # the core can never be drained by the sleeve


def test_sizing_rejects_ring_fence_violations():
    with pytest.raises(ValueError):
        moonshot_sizing(0.0)  # non-positive capital
    with pytest.raises(ValueError):
        moonshot_sizing(10_000.0, sleeve_fraction=0.10)  # exceeds the 5% hard cap
    with pytest.raises(ValueError):
        moonshot_sizing(10_000.0, sleeve_fraction=0.0)  # non-positive fraction
    with pytest.raises(ValueError):
        moonshot_sizing(10_000.0, max_bets=0)  # <1 bet


def test_gate_defaults_to_not_ready_and_lists_every_unmet_condition():
    g = moonshot_gate()
    assert g["ready"] is False
    # All three non-size conditions plus human approval are unmet by default.
    assert any("sub-account" in u for u in g["unmet"])
    assert any("max loss" in u for u in g["unmet"])
    assert any("human" in u for u in g["unmet"])


def test_gate_ready_only_when_fully_configured_and_human_approved():
    # Everything but human approval: still closed (Path-B activation is human-gated).
    almost = moonshot_gate(
        separate_subaccount=True, per_bet_max_loss_defined=True, human_approved=False
    )
    assert almost["ready"] is False
    assert any("human" in u for u in almost["unmet"])

    ok = moonshot_gate(
        separate_subaccount=True, per_bet_max_loss_defined=True, human_approved=True
    )
    assert ok["ready"] is True
    assert ok["unmet"] == []


def test_gate_rejects_oversize_sleeve_even_when_otherwise_approved():
    g = moonshot_gate(
        sleeve_fraction=0.20,
        separate_subaccount=True,
        per_bet_max_loss_defined=True,
        human_approved=True,
    )
    assert g["ready"] is False
    assert any("cap" in u for u in g["unmet"])


def test_build_defaults_to_not_ready():
    rec = build_moonshot_sleeve()
    assert rec["gate"]["ready"] is False
    assert rec["sizing"]["sleeve_fraction"] <= SLEEVE_MAX_FRACTION


def test_every_constraint_is_populated_and_sourced_to_a_real_file():
    assert CONSTRAINTS
    for c in CONSTRAINTS:
        assert c.constraint.strip()
        assert c.rule.strip()
        assert c.why_it_matters.strip()
        # The source is internal: the cited file must actually exist in the repo
        # (roadmap rows append a section after the path, so check the path prefix).
        path = c.source.split(" ", 1)[0]
        assert (REPO_ROOT / path).exists(), f"missing source file: {path}"


def test_markdown_renders_gate_loss_bound_and_every_constraint():
    rec = build_moonshot_sleeve()
    md = to_markdown(rec)
    assert md.startswith("# hl-bot — moonshot sleeve spec (B17)")
    assert "NOT READY" in md
    assert "Worst-case total loss" in md
    for c in CONSTRAINTS:
        assert c.constraint in md


def test_markdown_shows_ready_when_fully_configured():
    rec = build_moonshot_sleeve(
        separate_subaccount=True, per_bet_max_loss_defined=True, human_approved=True
    )
    assert "## Gate: READY" in to_markdown(rec)


def test_export_writes_valid_json_and_markdown(tmp_path):
    jp, mp = export(tmp_path)
    assert jp.exists() and mp.exists()
    loaded = json.loads(jp.read_text())
    assert loaded["gate"]["ready"] is False
    assert len(loaded["constraints"]) == len(CONSTRAINTS)
    assert loaded["sizing"]["worst_case_total_loss"] == loaded["sizing"]["sleeve_budget"]
    assert mp.read_text().startswith("# hl-bot — moonshot sleeve spec (B17)")
