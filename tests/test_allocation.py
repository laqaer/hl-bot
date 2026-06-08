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
