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

    def __init__(self, name: str, config: dict[str, Any] | None = None) -> None:
        self.name = name
        self.config = config or {}

    @abc.abstractmethod
    def decide(self, view: MarketView) -> list[Decision]:
        """Return zero or more decisions for this tick."""
        ...
