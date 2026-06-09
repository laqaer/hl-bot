"""`hlbot confirm/backtest --params` config-override parsing.

The factories hardcoded ``config={}``, so a parameter sweep (the 1d ``lookback_bars``
sweep the majors-momentum lead needs, B-horizon) meant editing code. ``_parse_agent_params``
turns a ``key=value,key=value`` CLI string into a typed override dict; this pins the
type inference (int → float → bool → str) and the error cases.
"""

from __future__ import annotations

import pytest

from hl_bot.cli.main import _parse_agent_params


def test_empty_string_is_empty_dict():
    assert _parse_agent_params("") == {}
    assert _parse_agent_params("   ") == {}


def test_int_float_bool_str_inference():
    cfg = _parse_agent_params("lookback_bars=7,enter_return=0.05,reversion=true,tag=alts")
    assert cfg == {
        "lookback_bars": 7,
        "enter_return": 0.05,
        "reversion": True,
        "tag": "alts",
    }
    assert isinstance(cfg["lookback_bars"], int)
    assert isinstance(cfg["enter_return"], float)
    assert cfg["reversion"] is True


def test_bool_false_and_whitespace_tolerated():
    cfg = _parse_agent_params(" regime_gate = false , top_k = 3 ")
    assert cfg == {"regime_gate": False, "top_k": 3}


def test_negative_number():
    assert _parse_agent_params("regime_min_return=-0.01") == {"regime_min_return": -0.01}


def test_missing_equals_raises():
    with pytest.raises(ValueError):
        _parse_agent_params("lookback_bars")


def test_empty_key_raises():
    with pytest.raises(ValueError):
        _parse_agent_params("=7")
