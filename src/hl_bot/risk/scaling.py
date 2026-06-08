"""Account-size based notional risk scaling.

The live bot uses notional caps rather than margin-only sizing. The approved
rule is now dynamic and uncapped:

- total bot-open notional <= 5x live unified portfolio value
- each individual position <= 1x live unified portfolio value

For Hyperliquid, "portfolio value" means the visible usable collateral view:
perp account value from ``clearinghouseState.marginSummary.accountValue`` plus
USDC from ``spotClearinghouseState``. Historical ``equity_snapshots`` are only a
fallback for offline/paper calculations, because older snapshots may contain
perp-only account value.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NotionalCap:
    max_total_notional: float
    max_per_position_notional: float
    portfolio_value: float | None
    avg_account_value: float | None
    multiplier: float
    per_position_multiplier: float
    ceiling_notional: float | None
    lookback_days: int
    sample_count: int
    source: str


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def spot_usdc_from_state(spot_state: dict[str, Any] | None) -> float:
    """Extract USDC balance from Hyperliquid spotClearinghouseState."""
    if not spot_state:
        return 0.0
    for balance in spot_state.get("balances", []) or []:
        if balance.get("coin") == "USDC":
            return _to_float(balance.get("total"))
    return 0.0


def perp_account_value_from_state(clearinghouse_state: dict[str, Any] | None) -> float:
    """Extract perp account value from Hyperliquid clearinghouseState."""
    if not clearinghouse_state:
        return 0.0
    return _to_float((clearinghouse_state.get("marginSummary") or {}).get("accountValue"))


def unified_portfolio_value(
    clearinghouse_state: dict[str, Any] | None,
    spot_state: dict[str, Any] | None = None,
) -> float:
    """Return unified HL portfolio value used for live risk sizing."""
    return perp_account_value_from_state(clearinghouse_state) + spot_usdc_from_state(spot_state)


def compute_notional_cap(
    conn: sqlite3.Connection,
    *,
    now_ms: int | None = None,
    live_portfolio_value: float | None = None,
    live_account_value: float | None = None,
    lookback_days: int = 3,
    multiplier: float = 5.0,
    per_position_multiplier: float = 1.0,
    ceiling_notional: float | None = None,
) -> NotionalCap:
    """Return total and per-position notional caps for the current tick.

    Live unified portfolio value is authoritative when supplied. If it is not
    supplied, use the average ``account_value`` from recent equity snapshots.
    ``live_account_value`` remains as a backwards-compatible alias for old call
    sites, but live tick code should pass ``live_portfolio_value``.

    ``ceiling_notional=None`` means no fixed dollar ceiling; this is the current
    approved production mode. Supplying a numeric ceiling preserves the old
    clamped behavior for tests or emergency overrides.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    portfolio_value: float | None = None
    avg: float | None = None
    samples = 0
    source = "unavailable"

    if live_portfolio_value is not None and live_portfolio_value > 0:
        portfolio_value = float(live_portfolio_value)
        avg = portfolio_value
        samples = 1
        source = "live_portfolio_value"
    elif live_account_value is not None and live_account_value > 0:
        # Backwards-compatible fallback for old callers. Prefer
        # live_portfolio_value because HL has unified collateral and spot USDC
        # can be usable portfolio value.
        portfolio_value = float(live_account_value)
        avg = portfolio_value
        samples = 1
        source = "live_account_value"
    else:
        cutoff_ms = now_ms - int(lookback_days * 86_400_000)
        row = conn.execute(
            """SELECT AVG(account_value) AS avg_account_value, COUNT(*) AS sample_count
               FROM equity_snapshots
               WHERE ts_ms >= ? AND ts_ms <= ? AND account_value > 0""",
            (cutoff_ms, now_ms),
        ).fetchone()
        avg = float(row["avg_account_value"]) if row and row["avg_account_value"] is not None else None
        samples = int(row["sample_count"] or 0) if row else 0
        if avg is not None:
            portfolio_value = float(avg)
            source = "equity_snapshots"

    if portfolio_value is None or portfolio_value <= 0:
        return NotionalCap(
            max_total_notional=0.0,
            max_per_position_notional=0.0,
            portfolio_value=None,
            avg_account_value=None,
            multiplier=float(multiplier),
            per_position_multiplier=float(per_position_multiplier),
            ceiling_notional=None if ceiling_notional is None else float(ceiling_notional),
            lookback_days=int(lookback_days),
            sample_count=0,
            source=source,
        )

    total = float(portfolio_value) * float(multiplier)
    if ceiling_notional is not None:
        total = min(float(ceiling_notional), total)

    return NotionalCap(
        max_total_notional=total,
        max_per_position_notional=float(portfolio_value) * float(per_position_multiplier),
        portfolio_value=float(portfolio_value),
        avg_account_value=float(avg) if avg is not None else None,
        multiplier=float(multiplier),
        per_position_multiplier=float(per_position_multiplier),
        ceiling_notional=None if ceiling_notional is None else float(ceiling_notional),
        lookback_days=int(lookback_days),
        sample_count=samples,
        source=source,
    )
