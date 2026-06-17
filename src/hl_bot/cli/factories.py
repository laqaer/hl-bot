"""Central agent factories for backtest, confirm, and live ticks.

This module ensures that `hlbot backtest`, `hlbot confirm`, and `hlbot femr_tick`
all instantiate agents with the same effective config: hardcoded defaults merged
with `configs/agent_overrides.json`. V3 provenance is enforced by computing a
`params_hash` from the merged config and persisting it in `agent_configs`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agents.base import Agent
from ..agents.basis import BasisAgent
from ..agents.femr import FemrAgent
from ..agents.funding_arb import FundingArbAgent
from ..agents.funding_carry import FundingCarryAgent
from ..agents.liq_cascade import LiqCascadeAgent
from ..agents.twap_mr import TwapMrAgent
from ..agents.twap_mr_regime import TwapMrRegimeAgent
from ..agents.veto import VetoAgent
from ..agents.xfund_carry import XFundCarryAgent
from ..config import CONFIG_DIR
from ..config_hash import hash_config

# Hardcoded defaults that the live tick historically layered on top of agent
# dataclass defaults. Keep them here so backtest/confirm use the same baseline.
AGENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "femr_v1": {
        "max_notional_per_trade": 20.0,
        "max_total_notional": 40.0,
        "funding_enter_per_hr": 0.00015,
        "funding_exit_per_hr": 0.00005,
    },
    "twap_mr_v1": {},
    "twap_mr_regime_v1": {},
    "liq_cascade_v1": {},
    "basis_v1": {},
    "funding_carry_v1": {},
    "xfund_carry_v1": {},
    "funding_arb_v1": {},
    "veto_v1": {},
}

AGENT_CLASSES: dict[str, type[Agent]] = {
    "femr_v1": FemrAgent,
    "twap_mr_v1": TwapMrAgent,
    "twap_mr_regime_v1": TwapMrRegimeAgent,
    "liq_cascade_v1": LiqCascadeAgent,
    "basis_v1": BasisAgent,
    "funding_carry_v1": FundingCarryAgent,
    "xfund_carry_v1": XFundCarryAgent,
    "funding_arb_v1": FundingArbAgent,
    "veto_v1": VetoAgent,
}

# Agents that can be G0-confirmed and promoted.
CONFIRMABLE_AGENTS: list[str] = [
    "femr_v1",
    "twap_mr_v1",
    "twap_mr_regime_v1",
    "liq_cascade_v1",
    "basis_v1",
    "funding_carry_v1",
    "xfund_carry_v1",
]

# Agents that always run in paper to accrue forward evidence.
PAPER_ROSTER: list[str] = [
    "femr_v1",
    "twap_mr_v1",
    "twap_mr_regime_v1",
    "liq_cascade_v1",
    "basis_v1",
]


def _load_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load runtime overrides from `configs/agent_overrides.json`."""
    path = path or (CONFIG_DIR / "agent_overrides.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_config(
    name: str,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Return the effective config for *name* and its V3 params_hash.

    *overrides* is a mapping `agent_name -> {param: value}`. If omitted, the live
    `configs/agent_overrides.json` file is used.
    """
    if name not in AGENT_CLASSES:
        raise KeyError(f"unknown agent {name}; known: {list(AGENT_CLASSES)}")

    merged = dict(AGENT_DEFAULTS.get(name, {}))
    ov = overrides if overrides is not None else _load_overrides()
    merged.update(ov.get(name) or {})
    return merged, hash_config(merged)


def agent_factory(
    name: str,
    conn: sqlite3.Connection | None = None,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> Agent:
    """Instantiate an agent with the effective config and persist its hash."""
    cfg, _ = agent_config(name, overrides=overrides)
    cls = AGENT_CLASSES[name]
    return cls(config=cfg, conn=conn)


def make_agent_factory(
    name: str,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> Callable[[sqlite3.Connection], Agent]:
    """Return a `conn -> Agent` factory suitable for backtest/confirm."""
    return lambda conn: agent_factory(name, conn=conn, overrides=overrides)


def list_confirmable_agents() -> list[str]:
    """Agents eligible for G0 confirmation."""
    return list(CONFIRMABLE_AGENTS)


def paper_roster() -> list[str]:
    """Agents that should always run in paper mode to accrue forward evidence."""
    return list(PAPER_ROSTER)
