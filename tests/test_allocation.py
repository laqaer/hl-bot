"""Tests for per-agent notional cap resolution.

Bug history:
  * The tick wired the MetaAllocator with max_alloc == 5x portfolio, letting a
    single agent consume the entire portfolio cap.
  * It also overwrote each agent's per-trade cap with min(total, 1x portfolio),
    which silently raised TWAP from its configured $200 to ~$600.

resolve_agent_caps centralizes the rule: 5x is a *portfolio* cap; any single
agent is limited to 1x portfolio, and explicit (lower) configured caps win.
"""

from __future__ import annotations

import math

import pytest

from hl_bot.risk.allocation import AgentCap, resolve_agent_caps
from hl_bot.risk.scaling import NotionalCap


def _cap(portfolio: float, mult: float = 5.0, per_pos_mult: float = 1.0) -> NotionalCap:
    return NotionalCap(
        max_total_notional=portfolio * mult,
        max_per_position_notional=portfolio * per_pos_mult,
        portfolio_value=portfolio,
        avg_account_value=portfolio,
        multiplier=mult,
        per_position_multiplier=per_pos_mult,
        ceiling_notional=None,
        lookback_days=3,
        sample_count=1,
        source="live_portfolio_value",
    )


def test_single_agent_capped_to_one_x_portfolio_not_five_x():
    risk = _cap(600.0)  # 5x=3000 total, 1x=600 per position
    # Allocator handed this agent the full portfolio cap.
    allocs = {"twap_mr_v1": 3000.0}
    configured = {"twap_mr_v1": {"max_total_notional": math.inf,
                                 "max_notional_per_trade": 200.0}}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["twap_mr_v1"].max_total_notional == pytest.approx(600.0)


def test_configured_per_trade_cap_is_not_raised():
    risk = _cap(600.0)  # 1x = 600
    allocs = {"twap_mr_v1": 600.0}
    configured = {"twap_mr_v1": {"max_total_notional": math.inf,
                                 "max_notional_per_trade": 200.0}}

    caps = resolve_agent_caps(allocs, risk, configured)

    # Must stay at the configured $200, NOT be lifted to the $600 per-position max.
    assert caps["twap_mr_v1"].max_notional_per_trade == pytest.approx(200.0)


def test_explicit_low_total_cap_preserved():
    risk = _cap(600.0)
    allocs = {"femr_v1": 600.0}
    configured = {"femr_v1": {"max_total_notional": 40.0,
                              "max_notional_per_trade": 20.0}}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["femr_v1"].max_total_notional == pytest.approx(40.0)
    assert caps["femr_v1"].max_notional_per_trade == pytest.approx(20.0)


def test_legacy_huge_static_cap_replaced_by_dynamic_one_x():
    risk = _cap(600.0)
    allocs = {"twap_mr_v1": 3000.0}
    # Legacy $1000 broad ceiling should be treated as "no real cap" and replaced
    # by the dynamic 1x-portfolio per-agent ceiling.
    configured = {"twap_mr_v1": {"max_total_notional": 1000.0,
                                 "max_notional_per_trade": 200.0}}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["twap_mr_v1"].max_total_notional == pytest.approx(600.0)


def test_allocator_share_below_ceiling_is_respected():
    risk = _cap(600.0)
    # Allocator only gave this agent $150; that should be the binding cap.
    allocs = {"femr_v1": 150.0}
    configured = {"femr_v1": {"max_total_notional": math.inf,
                              "max_notional_per_trade": 20.0}}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["femr_v1"].max_total_notional == pytest.approx(150.0)
    assert isinstance(caps["femr_v1"], AgentCap)


def test_aggregate_sum_capped_to_five_x_portfolio():
    # Eight agents each at the 1x per-position ceiling: per-agent clamps alone
    # would allow an 8x-portfolio book. The 5x aggregate cap must scale it down.
    risk = _cap(100.0)  # 5x=500 total, 1x=100 per agent
    allocs = {f"a{i}": 100.0 for i in range(8)}
    configured = {f"a{i}": {"max_total_notional": math.inf,
                            "max_notional_per_trade": math.inf} for i in range(8)}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert sum(c.max_total_notional for c in caps.values()) == pytest.approx(500.0)
    # Proportional: every agent shrinks by the same factor (100 * 500/800).
    for c in caps.values():
        assert c.max_total_notional == pytest.approx(62.5)
        # Per-trade follows the scaled total down.
        assert c.max_notional_per_trade == pytest.approx(62.5)


def test_aggregate_scaling_preserves_relative_weights():
    risk = _cap(100.0, mult=2.0)  # total cap 200, per-agent 100
    allocs = {"big": 100.0, "mid": 80.0, "small": 70.0}  # sum 250 > 200
    configured = {a: {"max_total_notional": math.inf,
                      "max_notional_per_trade": math.inf} for a in allocs}

    caps = resolve_agent_caps(allocs, risk, configured)

    # Scale = 200/250 = 0.8, applied uniformly.
    assert caps["big"].max_total_notional == pytest.approx(80.0)
    assert caps["mid"].max_total_notional == pytest.approx(64.0)
    assert caps["small"].max_total_notional == pytest.approx(56.0)


def test_aggregate_scaling_never_raises_explicit_per_trade():
    # Ten agents over-cap by 2x -> totals halve to 50. A per-trade that tracked
    # the old total (100) clamps to the new total; an explicit $10 stays $10.
    risk = _cap(100.0)  # 5x=500, 1x=100
    allocs = {f"a{i}": 100.0 for i in range(10)}
    configured = {f"a{i}": {"max_total_notional": math.inf,
                            "max_notional_per_trade": math.inf} for i in range(10)}
    configured["a0"]["max_notional_per_trade"] = 10.0

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["a0"].max_notional_per_trade == pytest.approx(10.0)
    assert caps["a1"].max_total_notional == pytest.approx(50.0)
    assert caps["a1"].max_notional_per_trade == pytest.approx(50.0)


def test_aggregate_under_cap_is_unchanged():
    # A book inside the 5x cap must come back byte-identical — the aggregate
    # layer is tightening-only.
    risk = _cap(600.0)  # 5x=3000
    allocs = {"twap_mr_v1": 600.0, "femr_v1": 150.0}
    configured = {
        "twap_mr_v1": {"max_total_notional": math.inf, "max_notional_per_trade": 200.0},
        "femr_v1": {"max_total_notional": 40.0, "max_notional_per_trade": 20.0},
    }

    caps = resolve_agent_caps(allocs, risk, configured)

    assert caps["twap_mr_v1"] == AgentCap(max_total_notional=600.0, max_notional_per_trade=200.0)
    assert caps["femr_v1"] == AgentCap(max_total_notional=40.0, max_notional_per_trade=20.0)


def test_aggregate_zero_portfolio_yields_zero_caps_without_error():
    # No portfolio value -> compute_notional_cap returns 0/0; every agent caps
    # to zero and the aggregate layer must not divide by the zero book.
    risk = _cap(0.0)
    allocs = {"a": 50.0, "b": 50.0}
    configured = {a: {"max_total_notional": math.inf,
                      "max_notional_per_trade": math.inf} for a in allocs}

    caps = resolve_agent_caps(allocs, risk, configured)

    assert all(c.max_total_notional == 0.0 for c in caps.values())
