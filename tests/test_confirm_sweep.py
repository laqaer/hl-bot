"""Plateau-sweep harness tests.

A PASS at exactly one parameter value is almost always overfit to that value; a
real edge sits on a *plateau* — a contiguous run of neighbouring values that all
pass (B-pairs slice 2). ``sweep_param``/``classify_plateau`` make that one
repeatable rule. These tests use a synthetic ``evaluate`` so the classification
logic is exercised without the network or any agent.
"""

from __future__ import annotations

from hl_bot.backtest.confirm import (
    SweepPoint,
    classify_plateau,
    sweep_param,
)


def _eval_from(table: dict[object, tuple[bool, float | None]]):
    """Build a deterministic evaluate(value)->(passing, edge) from a lookup."""
    return lambda v: table[v]


def test_contiguous_run_is_a_plateau():
    # 36/48/72 all pass and are adjacent -> a robust plateau.
    table = {
        24: (False, -1.0),
        36: (True, 4.0),
        48: (True, 5.3),
        72: (True, 4.1),
        96: (False, 0.5),
    }
    res = sweep_param("lookback_bars", [24, 36, 48, 72, 96], _eval_from(table))
    assert res.plateau
    assert res.plateau_values == [36, 48, 72]
    assert any("robust plateau" in r for r in res.reasons)


def test_isolated_single_pass_is_a_knife_edge():
    # only 48 passes; its neighbours fail -> overfit knife-edge, not a plateau.
    table = {
        24: (False, -1.0),
        36: (False, -0.2),
        48: (True, 5.3),
        72: (False, -0.4),
        96: (False, -1.1),
    }
    res = sweep_param("lookback_bars", [24, 36, 48, 72, 96], _eval_from(table))
    assert not res.plateau
    assert res.plateau_values == []
    assert any("knife-edge" in r for r in res.reasons)


def test_two_separate_single_passes_are_not_a_plateau():
    # two passing values but not adjacent -> no run of >=2, still a knife-edge.
    table = {
        24: (True, 3.5),
        36: (False, -0.2),
        48: (True, 4.0),
        72: (False, -0.4),
    }
    res = sweep_param("lookback_bars", [24, 36, 48, 72], _eval_from(table))
    assert not res.plateau
    assert any("knife-edge" in r for r in res.reasons)


def test_no_value_passes():
    table = {1: (False, -1.0), 2: (False, -2.0)}
    res = sweep_param("entry_z", [1, 2], _eval_from(table))
    assert not res.plateau
    assert any("no value passes" in r for r in res.reasons)


def test_min_plateau_three_requires_three_adjacent():
    points = [
        SweepPoint(24, False, -1.0),
        SweepPoint(36, True, 4.0),
        SweepPoint(48, True, 5.0),
        SweepPoint(72, False, -0.3),
    ]
    # a run of 2 is a plateau by default ...
    assert classify_plateau(points)[0]
    # ... but not when at least 3 adjacent are required.
    is_plateau, vals = classify_plateau(points, min_plateau=3)
    assert not is_plateau
    assert vals == []


def test_classify_picks_the_longest_run():
    points = [
        SweepPoint(1, True, 3.0),    # run of 1
        SweepPoint(2, False, -1.0),
        SweepPoint(3, True, 4.0),    # run of 3 (the longest)
        SweepPoint(4, True, 5.0),
        SweepPoint(5, True, 4.5),
        SweepPoint(6, False, -1.0),
    ]
    is_plateau, vals = classify_plateau(points)
    assert is_plateau
    assert vals == [3, 4, 5]


def test_summary_renders_marks_and_verdict():
    table = {1: (True, 3.0), 2: (True, 4.0)}
    res = sweep_param("lookback_bars", [1, 2], _eval_from(table))
    text = res.summary()
    assert "PLATEAU" in text
    assert "lookback_bars" in text
    assert "✅" in text
