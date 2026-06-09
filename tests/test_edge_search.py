"""Edge-search summary tests — the canonical record stays internally consistent."""

from __future__ import annotations

import json

from hl_bot.reports.edge_search import (
    THESES,
    build_edge_search,
    export,
    to_markdown,
)


def test_theses_numbered_1_to_n_without_gaps():
    nums = [t.num for t in THESES]
    assert nums == list(range(1, len(THESES) + 1))


def test_backlog_keys_are_unique():
    keys = [t.key for t in THESES]
    assert len(keys) == len(set(keys))


def test_class_breakdown_matches_narrative():
    # Iter 38: eight directional + one execution + one cross-market; Iter 44/48
    # added the two cross-sectional factor ranks (low-vol BAB, illiquidity/Amihud).
    by_class: dict[str, int] = {}
    for t in THESES:
        by_class[t.klass] = by_class.get(t.klass, 0) + 1
    assert by_class == {
        "directional": 8,
        "execution": 1,
        "cross-market": 1,
        "cross-sectional": 2,
    }


def test_illiq_is_the_twelfth_thesis_and_pruned_on_the_third_window():
    # Iter 48 (B-illiq slice 4): the strongest lead since pairs is pruned because
    # the 3rd-window test sign-flips at the calendar-matched cadence.
    illiq = next(t for t in THESES if t.key == "B-illiq")
    assert illiq.num == 12
    assert illiq.klass == "cross-sectional"
    assert "SIGN-FLIP" in illiq.prune_reason.upper()


def test_every_thesis_has_a_prune_reason_and_headline():
    for t in THESES:
        assert t.prune_reason.strip()
        assert t.headline.strip()


def test_summary_reports_all_pruned():
    rec = build_edge_search()
    s = rec["summary"]
    assert s["n_theses"] == len(THESES)
    assert s["n_pruned"] == len(THESES)  # all pruned, none deployable
    assert s["by_class"]["directional"] == 8
    assert "search_boundary" in s


def test_markdown_renders_every_thesis():
    rec = build_edge_search()
    md = to_markdown(rec)
    assert md.startswith("# hl-bot edge-search summary")
    for t in THESES:
        assert t.key in md
        assert str(t.num) in md


def test_export_writes_valid_json_and_markdown(tmp_path):
    jp, mp = export(tmp_path)
    assert jp.exists() and mp.exists()
    loaded = json.loads(jp.read_text())
    assert len(loaded["theses"]) == len(THESES)
    assert loaded["summary"]["n_pruned"] == len(THESES)
    assert mp.read_text().startswith("# hl-bot edge-search summary")
