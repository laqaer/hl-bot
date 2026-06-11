"""Base Agent interface — every strategy implements `decide()`.

An Agent receives a MarketView and returns one or more Decisions. It does NOT
place orders itself; the runtime harness places them so paper/live routing,
guardrail checks, and logging are centralized.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from .decisions import Decision


@dataclass
class MarketView:
    """Snapshot of market state passed to an agent at decision time."""
    ts_ms: int
    mids: dict[str, float]                                # coin -> mid price
    funding: dict[str, float] = field(default_factory=dict)   # coin -> 1h funding rate
    open_interest: dict[str, float] = field(default_factory=dict)
    book_top: dict[str, tuple[float, float]] = field(default_factory=dict)  # coin -> (bid, ask)
    extra: dict[str, Any] = field(default_factory=dict)   # agent-specific signals


class Agent(abc.ABC):
    name: str

    # How this strategy's live ENTRIES should execute by default:
    # "maker" (post-only, earns the spread — patient carry/passive strategies)
    # or "taker" (market order, pays the spread — urgency-driven strategies).
    # Exits always stay taker for risk reduction. Per-agent override via the
    # "execution" key in agent config / agent_overrides.json; see femr_tick.
    default_execution: str = "taker"

    def __init__(self, name: str, config: dict[str, Any] | None = None) -> None:
        self.name = name
        self.config = config or {}

    @abc.abstractmethod
    def decide(self, view: MarketView) -> list[Decision]:
        """Return zero or more decisions for this tick."""
        ...

    def execution_mode(self) -> str:
        """Resolved entry execution for this agent: config override or default."""
        mode = str(self.config.get("execution", self.default_execution)).lower()
        return mode if mode in ("maker", "taker") else self.default_execution
