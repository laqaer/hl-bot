"""Example agent: funding-rate arbitrage skeleton.

NOT a production strategy. This is a reference implementation showing how to:
  - read funding rates from MarketView
  - produce paper Decisions
  - tag the cloid for attribution

If 1h funding > threshold, short the perp (collect funding).
If 1h funding < -threshold, long the perp (collect funding).
Otherwise, hold.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


class FundingArbAgent(Agent):
    def __init__(
        self,
        name: str = "funding_arb_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config, conn)
        self.threshold = float(self.config.get("funding_threshold_1h", 0.0001))  # 1bp/hr
        self.notional = float(self.config.get("notional_usd", 100))
        self.coins = self.config.get("coins", ["BTC", "ETH", "SOL"])

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        for coin in self.coins:
            f = view.funding.get(coin)
            mid = view.mids.get(coin)
            if f is None or mid is None or mid <= 0:
                continue
            if f > self.threshold:
                side, reason = "A", f"funding {f:.6f} > {self.threshold:.6f} -> short to collect"
            elif f < -self.threshold:
                side, reason = "B", f"funding {f:.6f} < {-self.threshold:.6f} -> long to collect"
            else:
                out.append(Decision(
                    agent=self.name, action="hold", coin=coin,
                    reasoning=f"funding {f:.6f} within band",
                    market_snapshot={"funding": f, "mid": mid},
                ))
                continue
            sz = round(self.notional / mid, 4)
            out.append(Decision(
                agent=self.name,
                action="place",
                coin=coin,
                side=side,
                sz=sz,
                px=mid,
                cloid=make_cloid(self.name),
                reasoning=reason,
                market_snapshot={"funding": f, "mid": mid, "notional": self.notional},
            ))
        return out
