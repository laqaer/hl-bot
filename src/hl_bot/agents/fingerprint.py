"""Stable config fingerprints for evidence provenance (backlog V3).

``hlbot confirm --record`` stamps a G0 pass into ``confirmations`` and the
supervisor's ``require_g0`` reads it back to gate promotion. But confirm
instantiates each agent with its DEFAULT params while the live runner may apply
``agent_overrides.json`` — so a tuned override could inherit a G0 stamp earned
for a DIFFERENT config (the audit's G1 finding). A ``params_hash`` of the
agent's EFFECTIVE config makes that mismatch detectable: stamp it on confirm,
match it in ``require_g0``.

The hash covers the resolved param dataclass (``agent.cfg`` — defaults with any
overrides applied) when the agent exposes one, else the raw override dict. Two
agents with identical effective params therefore hash identically regardless of
how those params were supplied.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any


def config_payload(agent: Any) -> dict[str, Any]:
    """The agent's effective, hashable config: its resolved param dataclass
    (``cfg``) if it exposes one, else the raw override dict."""
    cfg = getattr(agent, "cfg", None)
    if is_dataclass(cfg) and not isinstance(cfg, type):
        return asdict(cfg)
    raw = getattr(agent, "config", None)
    return dict(raw) if isinstance(raw, dict) else {}


def config_fingerprint(agent: Any) -> str:
    """Stable 12-hex-char SHA-256 of the agent's effective config.

    Deterministic and key-order independent (sorted keys), so identical params
    always produce the same hash and a changed param always changes it.
    """
    blob = json.dumps(
        config_payload(agent), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
