"""Base Agent interface — every strategy implements `decide()`.

An Agent receives a MarketView and returns one or more Decisions. It does NOT
place orders itself; the runtime harness places them so paper/live routing,
guardrail checks, and logging are centralized.
"""

from __future__ import annotations

import abc
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from ..config_hash import hash_config
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
    config: dict[str, Any]
    params_hash: str

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.name = name
        self.config = config or {}
        self.params_hash = hash_config(self.config)
        if conn is not None:
            self._persist_config(conn)

    def _persist_config(
        self,
        conn: sqlite3.Connection,
        source: str = "effective",
    ) -> None:
        """Upsert this agent's effective config into the registry."""
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_configs(
                agent, params_hash, config_json, created_ms, source
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                self.name,
                self.params_hash,
                json.dumps(self.config, separators=(",", ":")),
                int(time.time() * 1000),
                source,
            ),
        )

    @abc.abstractmethod
    def decide(self, view: MarketView) -> list[Decision]:
        """Return zero or more decisions for this tick."""
        ...
