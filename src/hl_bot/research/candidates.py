"""Paper-only candidate strategies and signal filters.

These are incubation-stage ideas. They are registered here so the bot can
evaluate them on paper, but they are NEVER wired to live capital unless they
explicitly pass their declared promotion gate (and a human merges the change).

First candidate: a trend/regime filter that tells the TWAP mean-reversion agent
when NOT to fade. Fading a strong directional breakout is the primary loss loop
we want to break; this is a pure, testable function that a paper wrapper (or, if
proven, the live TWAP agent) can consult before placing a fade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CandidateStrategy:
    name: str
    description: str
    based_on: str
    mode: str = "paper"
    enabled_live: bool = False
    promotion_gate: dict[str, Any] = field(default_factory=dict)


def is_trending(
    closes: list[float],
    *,
    min_move_pct: float = 0.03,
    min_consistency: float = 0.65,
) -> bool:
    """True when recent price action is a strong directional trend/breakout.

    Combines two signals over the close series:
      * net move magnitude (|last/first - 1|) exceeds ``min_move_pct``
      * directional consistency: the dominant step direction's share of all
        non-flat steps exceeds ``min_consistency``.

    Both must hold, so choppy/mean-reverting series are NOT flagged as trending.
    """
    if len(closes) < 4 or closes[0] <= 0:
        return False
    net = abs(closes[-1] / closes[0] - 1.0)
    if net < min_move_pct:
        return False
    ups = downs = 0
    for a, b in zip(closes, closes[1:], strict=False):
        if b > a:
            ups += 1
        elif b < a:
            downs += 1
    steps = ups + downs
    if steps == 0:
        return False
    consistency = max(ups, downs) / steps
    return consistency >= min_consistency


def regime_allows_fade(
    z: float,
    closes: list[float],
    *,
    min_move_pct: float = 0.03,
    min_consistency: float = 0.65,
) -> tuple[bool, str]:
    """Decide whether TWAP should be allowed to fade given the local regime.

    TWAP fades the deviation: z > 0 (price above VWAP) -> SHORT; z < 0 -> LONG.
    The filter blocks ONLY the fade that leans against a strong trend:
      * strong UPtrend + z > 0  -> would short into strength: BLOCK
      * strong DOWNtrend + z < 0 -> would long into weakness: BLOCK
    A fade aligned with the trend, or any fade in a choppy market, is allowed.

    Returns (allow, reason).
    """
    if not is_trending(closes, min_move_pct=min_move_pct, min_consistency=min_consistency):
        return True, "regime: choppy/range — fade allowed"
    trending_up = closes[-1] >= closes[0]
    if trending_up and z > 0:
        return False, "regime: strong uptrend — block short-fade into strength"
    if (not trending_up) and z < 0:
        return False, "regime: strong downtrend — block long-fade into weakness"
    return True, "regime: fade aligned with trend — allowed"


# ---------------------------------------------------------------------------
# Registry (paper-only, gated)
# ---------------------------------------------------------------------------

CANDIDATES: list[CandidateStrategy] = [
    CandidateStrategy(
        name="twap_mr_regime_v0",
        description=(
            "TWAP mean-reversion guarded by a trend/regime filter that suppresses "
            "fades placed against a strong directional breakout."
        ),
        based_on="twap_mr_v1",
        mode="paper",
        enabled_live=False,
        promotion_gate={
            # Must beat baseline TWAP on real sample before earning any size.
            "min_trades_30d": 200,
            "min_edge_bps_30d": 5,
            "min_net_pnl_30d": 50,
            "max_concentration": 0.5,
            "to_mode": "live_small",
        },
    ),
]
