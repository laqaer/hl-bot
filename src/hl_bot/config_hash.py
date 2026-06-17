"""Deterministic config serialization and content hashing.

V3 provenance requires that every agent decision, fill, and confirmation can be
tied to the exact parameter blob that produced it. We compute a short
content hash from the effective config (defaults + overrides) so that a config
change produces a new hash and invalidates stale confirmations.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _round_float(value: float, significant_digits: int = 10) -> float:
    """Round a float to a fixed number of significant digits.

    This eliminates tiny representation differences (e.g. 0.1 + 0.2) without
    changing the economic meaning of a config parameter.
    """
    if value == 0:
        return 0.0
    from math import floor, log10

    magnitude = floor(log10(abs(value)))
    decimals = significant_digits - 1 - magnitude
    return round(value, decimals)


def normalize_config(obj: Any) -> Any:
    """Return a deterministic, JSON-serializable copy of *obj*.

    - dict keys are sorted recursively;
    - floats are rounded to 10 significant digits;
    - ints, strings, bools, and None are left as-is;
    - lists are processed element-wise (order preserved).
    """
    if isinstance(obj, dict):
        return {k: normalize_config(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize_config(v) for v in obj]
    if isinstance(obj, float):
        return _round_float(obj)
    if isinstance(obj, (int, str, bool)) or obj is None:
        return obj
    # Fallback for anything unexpected (e.g. enums, decimals).
    return str(obj)


def hash_config(config: dict[str, Any]) -> str:
    """Return a 16-character hex content hash for *config*."""
    normalized = normalize_config(config)
    payload = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
