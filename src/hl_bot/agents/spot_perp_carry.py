"""Spot-Perp Carry (S4) — single-venue cash-and-carry on HL.

HL perp funding is pinned at a ~11% APR baseline that is almost always present.
Long HL **spot** + short the same coin's HL **perp**, 1:1 notional, collects that
funding market-neutral and continuously — no spike needed. Net edge = funding
collected − 4 legs of cost − basis drift (small, same-venue, mean-reverts by
arbitrage). See docs/research/S4_spot_perp_carry.md.

Leg representation:
  * perp leg — Decision.coin = the plain coin ("HYPE"), priced from mids["HYPE"],
    carries funding; we short it (side "A") to collect positive funding.
  * spot leg — Decision.coin = "HYPE-SPOT", priced from mids["HYPE-SPOT"], NO
    funding; we go long (side "B").

Leg sequencing (leg-out risk control): the SPOT leg is placed first; the PERP
leg is emitted only after the spot fill shows up in the decision audit log. This
agent is backtest + paper capable; live spot-order execution is a later PR.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision

HOURS_PER_YEAR = 8760.0
SPOT_SUFFIX = "-SPOT"


@dataclass
class SpotPerpCarryConfig:
    # Enter at/below the 11% APR baseline by design — being IN during ordinary
    # baseline funding is the whole point (a spike just makes it richer).
    enter_apr: float = 0.10              # enter when perp funding annualized >= this
    exit_apr: float = 0.0                # exit when funding annualized < this
    lookback_h: float = 24.0             # mean funding over this trailing window
    min_daily_volume_usd: float = 10_000_000.0
    basis_stop_bps: float = 50.0         # unwind if |spot-perp|/perp exceeds this
    max_hold_hours: float = 2160.0       # 90d: baseline carry holds; the
                                         # 14d default churned taker exits
    max_notional_per_trade: float = 25.0     # PER LEG
    max_total_notional: float = 75.0         # across perp legs (the risk-bearing side)
    max_concurrent_positions: int = 5        # more concurrent holds = enough
                                             # round trips to clear the G0 min-trade gate


class SpotPerpCarryAgent(Agent):
    default_execution = "maker"  # maker-first both legs: the economics need it

    def __init__(
        self,
        name: str = "spot_perp_carry_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = SpotPerpCarryConfig(
            enter_apr=float(c.get("enter_apr", 0.10)),
            exit_apr=float(c.get("exit_apr", 0.0)),
            lookback_h=float(c.get("lookback_h", 24.0)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            basis_stop_bps=float(c.get("basis_stop_bps", 50.0)),
            max_hold_hours=float(c.get("max_hold_hours", 2160.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 75.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 5)),
        )
        self.conn = conn
        # coin -> trailing list of (ts_ms, hourly_funding_rate); the lookback mean.
        self._fhist: dict[str, list[tuple[int, float]]] = {}

    # -- helpers ----------------------------------------------------------
    def _open_positions(self) -> dict[str, dict]:
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px
               FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
                 AND is_paper = ?
               ORDER BY ts_ms ASC""",
            (self.name, 0 if self.is_live else 1),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {"ts_ms": r["ts_ms"], "side": r["side"],
                                      "sz": float(r["sz"] or 0), "entry_px": float(r["px"] or 0)}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    @staticmethod
    def _spot_mid(view: MarketView, coin: str) -> float | None:
        """Spot mid for ``coin``: backtest puts it in mids[-SPOT]; live-paper
        enrichment puts it in extra["spot_mids"][coin]. Support both."""
        m = view.mids.get(f"{coin}{SPOT_SUFFIX}")
        if m is None:
            m = (view.extra.get("spot_mids") or {}).get(coin)
        return m

    def _funding_apr(self, view: MarketView, coin: str) -> float | None:
        """Annualized funding from the trailing lookback mean (instantaneous if
        no history). Updates ``self._fhist`` for coins with a perp mid + funding."""
        funding = view.funding or {}
        f = funding.get(coin)
        if f is None:
            hist = self._fhist.get(coin)
            return (sum(r for _, r in hist) / len(hist)) * HOURS_PER_YEAR if hist else None
        hist = self._fhist.setdefault(coin, [])
        hist.append((view.ts_ms, f))
        cutoff = view.ts_ms - int(self.cfg.lookback_h * 3_600_000)
        self._fhist[coin] = [(t, r) for t, r in hist if t >= cutoff]
        kept = self._fhist[coin]
        mean = (sum(r for _, r in kept) / len(kept)) if kept else f
        return mean * HOURS_PER_YEAR

    # -- decide -----------------------------------------------------------
    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        funding = view.funding or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}

        # Refresh the lookback history for every coin that quotes a perp + funding,
        # even ones we don't act on, so the trailing mean is complete.
        for coin in list(funding):
            if (view.mids.get(coin) or 0) > 0:
                self._funding_apr(view, coin)

        open_pos = self._open_positions()
        held_logical = {c for c in open_pos if not c.endswith(SPOT_SUFFIX)}
        held_logical |= {c[: -len(SPOT_SUFFIX)] for c in open_pos if c.endswith(SPOT_SUFFIX)}

        # ---- exits (both legs of a logical position) ----
        flattening: set[str] = set()      # logical coins being unwound this cycle
        for coin in sorted(held_logical):
            perp = open_pos.get(coin)
            spot = open_pos.get(f"{coin}{SPOT_SUFFIX}")
            perp_mid = view.mids.get(coin)
            spot_mid = self._spot_mid(view, coin)
            apr = self._funding_apr(view, coin)
            ref_ts = (perp or spot or {}).get("ts_ms")
            held_hrs = ((time.time() - ref_ts / 1000) / 3600) if ref_ts else 0.0
            basis_bps = (
                abs(perp_mid - spot_mid) / perp_mid * 1e4
                if perp_mid and spot_mid and perp_mid > 0 else 0.0
            )
            reason = None
            if apr is not None and apr < 0:
                reason = f"FUNDING-FLIPPED ({apr*100:+.1f}% APR)"
            elif apr is not None and apr < self.cfg.exit_apr:
                reason = f"CARRY-STOPPED ({apr*100:+.1f}% APR < {self.cfg.exit_apr*100:.1f}%)"
            elif basis_bps > self.cfg.basis_stop_bps:
                reason = f"BASIS-STOP ({basis_bps:.1f}bps)"
            elif held_hrs >= self.cfg.max_hold_hours:
                reason = f"MAX-HOLD ({held_hrs:.1f}h)"
            if not reason:
                continue
            flattening.add(coin)
            # Flatten BOTH open legs at their respective mids.
            if perp is not None and perp_mid:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=perp["sz"], px=perp_mid, cloid=make_cloid(self.name),
                    reasoning=f"S4 EXIT {coin}: {reason}",
                    market_snapshot={"leg": "perp", "apr": apr, "basis_bps": basis_bps,
                                     "held_hrs": held_hrs},
                ))
            if spot is not None and spot_mid:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=f"{coin}{SPOT_SUFFIX}",
                    sz=spot["sz"], px=spot_mid, cloid=make_cloid(self.name),
                    reasoning=f"S4 EXIT {coin}{SPOT_SUFFIX}: {reason}",
                    market_snapshot={"leg": "spot", "apr": apr, "basis_bps": basis_bps,
                                     "held_hrs": held_hrs},
                ))

        # ---- complete pending hedges FIRST (leg-out control) ----
        # A spot leg that has already filled MUST get its perp hedge, regardless
        # of the entry threshold. Gating completion on enter_apr would leave us
        # long spot UNHEDGED whenever the entry signal eases between the two legs
        # (Codex review). If we no longer want the position at all, the exit
        # block above already flattened the spot leg (it's in `flattening`).
        perp_held = {c for c in open_pos if not c.endswith(SPOT_SUFFIX)}
        spot_only = {c[: -len(SPOT_SUFFIX)] for c in open_pos if c.endswith(SPOT_SUFFIX)} - perp_held
        for coin in sorted(spot_only - flattening):
            perp_mid = view.mids.get(coin)
            f = funding.get(coin)
            if not perp_mid or perp_mid <= 0 or f is None or f <= 0:
                # funding no longer positive -> never short a perp into it; the
                # exit block unwinds the dangling spot leg instead.
                continue
            spot_leg = open_pos[f"{coin}{SPOT_SUFFIX}"]
            spot_mid = self._spot_mid(view, coin) or spot_leg["entry_px"]
            # size the perp to the spot leg's live notional -> true dollar-neutral
            sz = round(spot_leg["sz"] * spot_mid / perp_mid, 5)
            if sz <= 0:
                continue
            apr = self._funding_apr(view, coin)
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side="A", sz=sz,
                px=perp_mid, cloid=make_cloid(self.name),
                reasoning=(f"S4 HEDGE perp {coin} @ ${perp_mid:.4f} short "
                           f"({(apr or 0)*100:.0f}% APR) [leg 2/2]"),
                market_snapshot={"leg": "perp", "apr": apr, "spot_mid": spot_mid,
                                 "perp_mid": perp_mid, "hedge": True},
            ))

        # ---- open NEW positions (spot leg first; the hedge above completes it
        #      next tick). Logical positions in flight = held perp legs + spot-
        #      only legs awaiting their perp; reserve a clip of perp room each. ----
        active_logical = (perp_held | spot_only) - flattening
        room = self.cfg.max_concurrent_positions - len(active_logical)
        active_perp_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items()
            if not c.endswith(SPOT_SUFFIX) and c not in flattening
        )
        reserved = len(spot_only - flattening) * self.cfg.max_notional_per_trade
        room_notional = self.cfg.max_total_notional - active_perp_notional - reserved

        candidates: list[tuple[str, float]] = []
        for coin in funding:
            if coin in flattening or coin.endswith(SPOT_SUFFIX):
                continue
            if coin in open_pos or f"{coin}{SPOT_SUFFIX}" in open_pos:
                continue                      # already in a position (or hedging)
            perp_mid = view.mids.get(coin)
            spot_mid = self._spot_mid(view, coin)
            if not perp_mid or perp_mid <= 0 or spot_mid is None or spot_mid <= 0:
                continue
            f = funding.get(coin)
            if f is None or f <= 0:          # we short perp -> funding must be positive
                continue
            apr = self._funding_apr(view, coin)
            if apr is None or apr < self.cfg.enter_apr:
                continue
            if vol.get(coin, 0) < self.cfg.min_daily_volume_usd:
                continue
            candidates.append((coin, apr))
        candidates.sort(key=lambda kv: kv[1], reverse=True)

        for coin, apr in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            spot_mid = self._spot_mid(view, coin)  # checked non-None above
            perp_mid = view.mids[coin]
            clip = min(self.cfg.max_notional_per_trade, room_notional)
            if clip < 5.0:
                continue
            sz = round(clip / spot_mid, 5)
            out.append(Decision(
                agent=self.name, action="place", coin=f"{coin}{SPOT_SUFFIX}",
                side="B", sz=sz, px=spot_mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"S4 ENTER spot {coin}{SPOT_SUFFIX} @ ${spot_mid:.4f} "
                    f"({apr*100:.0f}% APR), notional ${clip:.2f} [leg 1/2]"
                ),
                market_snapshot={"leg": "spot", "apr": apr, "spot_mid": spot_mid,
                                 "perp_mid": perp_mid, "notional": clip},
            ))
            room -= 1
            room_notional -= clip

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(f"S4 idle: {len(candidates)} candidate(s), "
                           f"{len(held_logical)} held"),
                market_snapshot={},
            ))
        return out
