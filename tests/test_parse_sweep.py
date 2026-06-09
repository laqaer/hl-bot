"""`hlbot confirm --sweep` value-grid parsing.

``--sweep 'lookback_bars=24,36,48,72'`` checks a PASS is a robust plateau, not a
single-point knife-edge (B-pairs slice 2). ``_parse_sweep`` turns that one-param,
many-value CLI string into ``(param, [values])`` with the same int→float→bool→str
inference as ``--params``; this pins the typing and the error cases.
"""

from __future__ import annotations

import pytest

from hl_bot.cli.main import _parse_sweep


def test_parses_param_and_typed_int_values():
    param, values = _parse_sweep("lookback_bars=24,36,48,72")
    assert param == "lookback_bars"
    assert values == [24, 36, 48, 72]


def test_float_values_are_typed():
    param, values = _parse_sweep("entry_z=1.5,2.0,2.5")
    assert param == "entry_z"
    assert values == [1.5, 2.0, 2.5]


def test_whitespace_is_tolerated():
    param, values = _parse_sweep("  lookback_bars = 24, 48 ")
    assert param == "lookback_bars"
    assert values == [24, 48]


def test_missing_equals_raises():
    with pytest.raises(ValueError):
        _parse_sweep("lookback_bars")


def test_empty_param_raises():
    with pytest.raises(ValueError):
        _parse_sweep("=24,48")


def test_single_value_raises():
    # a sweep with one value can't show a plateau; require >=2.
    with pytest.raises(ValueError):
        _parse_sweep("lookback_bars=48")
