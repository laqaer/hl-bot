"""Resolve effective per-agent notional caps for a live tick.

The approved live risk rule has two layers:

* The *portfolio* may carry up to ``multiplier`` x (default 5x) live unified
  portfolio value of aggregate bot-open notional.
* Any *single agent* is limited to 1x portfolio value (``max_per_position``)
  unless it explicitly configures a smaller cap.

This module is the single source of truth for turning the MetaAllocator's
suggested split + each agent's configured caps into the numbers that actually
get written onto the agent before its turn. Keeping it pure makes the rule
unit-testable instead of buried in the CLI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .scaling import NotionalCap

# A configured ``max_total_notional`` at or above this is treated as a legacy
# blanket ceiling (the old static $1000) rather than a real per-agent limit, and
# is replaced by the dynamic 1x-portfolio per-agent ceiling.
LEGACY_TOTAL_THRESHOLD = 1000.0


@dataclass(frozen=True)
class AgentCap:
    max_total_notional: float
    max_notional_per_trade: float


def _is_finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def resolve_agent_caps(
    allocs: dict[str, float],
    risk_cap: NotionalCap,
    configured: dict[str, dict[str, float]],
    *,
    legacy_total_threshold: float = LEGACY_TOTAL_THRESHOLD,
) -> dict[str, AgentCap]:
    """Compute the binding total/per-trade caps for every agent.

    Args:
        allocs: MetaAllocator output (agent -> suggested total notional).
        risk_cap: live portfolio-derived caps (5x total / 1x per position).
        configured: agent -> {max_total_notional, max_notional_per_trade}
            as merged from defaults + overrides. Values may be inf/None.

    Returns:
        agent -> AgentCap with the final, safe numbers.
    """
    per_agent_ceiling = float(risk_cap.max_per_position_notional)
    out: dict[str, AgentCap] = {}

    for name, alloc in allocs.items():
        cfg = configured.get(name) or {}
        configured_total = cfg.get("max_total_notional")
        configured_per_trade = cfg.get("max_notional_per_trade")

        # Decide the per-agent total ceiling.
        if _is_finite_positive(configured_total) and configured_total < legacy_total_threshold:
            # Explicit, smaller-than-legacy cap: honor it.
            approved_total = float(configured_total)
        else:
            # Missing / infinite / legacy blanket cap -> dynamic 1x portfolio.
            approved_total = per_agent_ceiling

        total = min(float(alloc), approved_total, per_agent_ceiling)
        total = max(0.0, total)

        # Per-trade cap: never exceed the agent total nor the 1x-position max,
        # and preserve an explicit (lower) configured per-trade size.
        per_trade_ceiling = min(total, per_agent_ceiling)
        if _is_finite_positive(configured_per_trade):
            per_trade = min(float(configured_per_trade), per_trade_ceiling)
        else:
            per_trade = per_trade_ceiling

        out[name] = AgentCap(max_total_notional=total, max_notional_per_trade=per_trade)

    return out
