"""Agent decision logger — every action an agent considers gets recorded.

Used as the audit log for scoring (decision -> fill linkage via cloid) and
later for replay / backtesting agent reasoning.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["place", "cancel", "hold", "flatten", "error"]


@dataclass
class Decision:
    agent: str
    action: Action
    coin: str | None = None
    side: str | None = None              # 'B' / 'A'
    sz: float | None = None
    px: float | None = None
    cloid: str | None = None
    reasoning: str | None = None
    market_snapshot: dict[str, Any] = field(default_factory=dict)
    is_paper: bool = True
    error: str | None = None


def log_decision(conn: sqlite3.Connection, d: Decision) -> int:
    cur = conn.execute(
        """
        INSERT INTO agent_decisions(
            ts_ms, agent, action, coin, side, sz, px, cloid,
            reasoning, market_snapshot, is_paper, error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(time.time() * 1000),
            d.agent,
            d.action,
            d.coin,
            d.side,
            d.sz,
            d.px,
            d.cloid,
            d.reasoning,
            json.dumps(d.market_snapshot, separators=(",", ":")),
            1 if d.is_paper else 0,
            d.error,
        ),
    )
    return cur.lastrowid or 0
