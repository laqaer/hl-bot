"""TWAP-MR + regime filter — don't fade a strong trend.

The plain ``TwapMrAgent`` fades any >2-sigma deviation from VWAP. In crypto the
single biggest loss loop is fading a *breakout*: price rips, the agent shorts the
strength, price keeps ripping, stop-out, repeat. ``research/candidates.py`` already
has the cure (``regime_allows_fade``) but nothing consulted it.

This agent wraps the baseline: it generates the same fade decisions, then drops
any fade that leans against a strong directional trend (short into an uptrend /
long into a downtrend). A fade in a choppy/range market — where mean reversion
actually works — is allowed through unchanged.

It needs a recent close series per coin in ``view.extra['closes'][coin]`` (the
backtest data loader and the live ``runtime.enrich_view`` both populate it).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..research.candidates import regime_allows_fade
from .base import MarketView
from .decisions import Decision
from .twap_mr import TwapMrAgent


class TwapMrRegimeAgent(TwapMrAgent):
    def __init__(
        self,
        name: str = "twap_mr_regime_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config, conn)
        c = config or {}
        self.regime_min_move_pct = float(c.get("regime_min_move_pct", 0.03))
        self.regime_min_consistency = float(c.get("regime_min_consistency", 0.65))

    def decide(self, view: MarketView) -> list[Decision]:
        decisions = super().decide(view)
        closes_by_coin: dict[str, list[float]] = view.extra.get("closes", {}) or {}
        out: list[Decision] = []
        blocked = 0
        for d in decisions:
            if d.action == "place" and d.coin:
                z = float((d.market_snapshot or {}).get("z", 0.0))
                closes = closes_by_coin.get(d.coin) or []
                allow, reason = regime_allows_fade(
                    z, closes,
                    min_move_pct=self.regime_min_move_pct,
                    min_consistency=self.regime_min_consistency,
                )
                if not allow:
                    blocked += 1
                    out.append(Decision(
                        agent=self.name, action="hold", coin=d.coin,
                        reasoning=f"REGIME-BLOCK {d.coin}: {reason}",
                        market_snapshot={**(d.market_snapshot or {}), "regime_blocked": True},
                    ))
                    continue
            out.append(d)
        if blocked:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=f"regime filter blocked {blocked} fade(s) against trend",
                market_snapshot={"regime_blocked_count": blocked},
            ))
        return out
