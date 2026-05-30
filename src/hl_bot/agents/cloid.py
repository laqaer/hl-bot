"""Cloid encoding for agent attribution.

Hyperliquid client order ids are 128-bit hex strings ("0x" + 32 hex chars).
We pack:
  bytes [0:2]  = 0xA9 0xE1  (magic, "agent")
  bytes [2:6]  = first 4 bytes of sha1(agent_name).hex()
  bytes [6:16] = random 10 bytes

Lookup table maps agent_name <-> 4-byte prefix so we can resolve fills.
"""

from __future__ import annotations

import hashlib
import secrets

MAGIC = "a9e1"


def agent_prefix(agent_name: str) -> str:
    """Stable 4-byte (8-hex-char) prefix for an agent name."""
    return hashlib.sha1(agent_name.encode()).hexdigest()[:8]


def make_cloid(agent_name: str) -> str:
    """Generate a new cloid tagged with the agent name."""
    return "0x" + MAGIC + agent_prefix(agent_name) + secrets.token_hex(10)


def agent_from_cloid(cloid: str | None, known_agents: list[str] | None = None) -> str:
    """Resolve an agent name from a cloid. Returns 'manual' if not ours."""
    if not cloid:
        return "manual"
    h = cloid.lower().removeprefix("0x")
    if not h.startswith(MAGIC) or len(h) != 32:
        return "manual"
    prefix = h[4:12]
    if known_agents:
        for name in known_agents:
            if agent_prefix(name) == prefix:
                return name
    # Fall back to prefix-tagged unknown so we don't lose attribution.
    return f"unknown:{prefix}"
