"""Base Agent interface — every strategy implements `decide()`.

An Agent receives a MarketView and returns one or more Decisions. It does NOT
place orders itself; the runtime harness places them so paper/live routing,
guardrail checks, and logging are centralized.
"""

from __future__ import annotations

import abc
import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .decisions import Decision


def compute_params_hash(params: Mapping[str, Any]) -> str:
    """Stable 12-hex-char fingerprint of a params dict (canonical JSON).

    Order-independent (keys sorted) and type-tolerant (``default=str`` for any
    odd value) so the SAME effective config always hashes the same — the basis
    for matching a G0 confirmation to the CURRENTLY DEPLOYED config (V3).
    """
    canonical = json.dumps(dict(params), sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


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

    # Set by the engine's roster split each cycle: True only when the agent is
    # in the LIVE execution roster. Position replays filter agent_decisions by
    # is_paper accordingly — the paper simulator writes place/flatten audit
    # rows for the SAME agent names, and replaying the wrong universe makes a
    # promoted agent "own" paper positions (and try to close them live).
    is_live: bool = False

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

    def params_fingerprint(self) -> dict[str, Any]:
        """The behaviour-determining params this agent actually runs with.

        Defaults to the resolved ``cfg`` dataclass (defaults + overrides merged
        at construction) so two configs that resolve to identical behaviour hash
        the same; falls back to the raw ``config`` when an agent has no ``cfg``.
        Override only if an agent's behaviour depends on state outside ``cfg``.
        """
        cfg = getattr(self, "cfg", None)
        if cfg is not None and dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
            return dataclasses.asdict(cfg)
        return dict(self.config)

    def params_hash(self) -> str:
        """Provenance hash of the deployed config — stamped into a G0
        confirmation and matched by promotion's ``require_g0`` (V3)."""
        return compute_params_hash(self.params_fingerprint())
