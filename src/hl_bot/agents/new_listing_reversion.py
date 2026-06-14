"""New-listing day-1 reversion — fade the listing-day overshoot (moonshot sleeve).

D2(b). New HL perp listings have a recurring day-1 dynamic: forced price
discovery on a thin book with no funding history overshoots fair value (a pop or
a dump), then mean-reverts over the following hours as two-sided liquidity
arrives. This is the same forced/emotional-flow → overshoot → revert structure
as ``dislocation_reversion`` (D1), but the *reference* is the listing price
rather than a rolling VWAP — on day 1 there is not yet enough history for a VWAP
(the vwap warmup is 30+ bars; day-1 at 1h is < 24 bars), so the standard z-score
signal does not exist. The listing reference price is the only fair-value anchor
available that early.

Signal (backtest only for now — see the LIVE note below):
  * ``view.extra['new_listings'][coin]`` = ``{age_bars, ref_px, vol_usd,
    recent_closes}`` — emitted by ``build_frames`` for coins whose candle history
    begins materially after the dataset's retention-cliff anchor (i.e. listed
    *during* the window). ``ref_px`` is the first traded close (the listing
    reference); ``age_bars`` is bars since listing; ``vol_usd`` is rolling-24h
    notional since listing.

Rule (symmetric fade of the day-1 overshoot, only while ``age_bars`` is within
day 1):
  * mid gapped FAR ABOVE the listing reference (runup >= +min_runup) -> SHORT,
  * mid gapped FAR BELOW (runup <= -min_runup)                       -> LONG.
Exit on revert toward the reference (``|runup| <= exit_runup``), a hard stop
(new listings are violent — the "it keeps mooning" tail is the kill, so the stop
is wider than dislocation's 2%), or max-hold.

Perp-only, TAKER entry (a reversion must get in now; resting maker on a thin
day-1 book suffers brutal adverse selection). Hard-capped sleeve sizing — this
is a moonshot sleeve, not a core book (docs/ALPHA_ROADMAP.md S7).

LIVE wiring (deferred — honest gap): ``new_listings`` is computed in the
backtest frame assembly from full candle history. The live ``build_view`` does
NOT populate it yet, so this agent HOLDS in live until first-seen listing
timestamps are tracked forward (e.g. from the perp ``meta`` each cycle). Wiring
that is only worth doing if the backtest confirms — so it is gated on the G0
verdict, not done speculatively.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


@dataclass
class NewListingReversionConfig:
    max_age_bars: int = 24          # only ENTER during day 1 (24 bars at 1h)
    min_runup: float = 0.25         # fade only a >=25% gap from the listing ref
    exit_runup: float = 0.08        # take profit when within 8% of the listing ref
    stop_pct: float = 0.08          # wide hard stop — day-1 moves are violent
    max_hold_bars: int = 24         # reversion horizon (~1 day at 1h)
    bar_seconds: int = 3600         # 1h
    min_listing_vol_usd: float = 1_000_000.0   # skip illiquid micro-listings
    max_notional_per_trade: float = 15.0       # moonshot sleeve — small
    max_total_notional: float = 30.0
    max_concurrent_positions: int = 2


class NewListingReversionAgent(Agent):
    default_execution = "taker"  # a reversion must get in now (same as dislocation)

    def __init__(
        self,
        name: str = "new_listing_reversion_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = NewListingReversionConfig(
            max_age_bars=int(c.get("max_age_bars", 24)),
            min_runup=float(c.get("min_runup", 0.25)),
            exit_runup=float(c.get("exit_runup", 0.08)),
            stop_pct=float(c.get("stop_pct", 0.08)),
            max_hold_bars=int(c.get("max_hold_bars", 24)),
            bar_seconds=int(c.get("bar_seconds", 3600)),
            min_listing_vol_usd=float(c.get("min_listing_vol_usd", 1_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 15.0)),
            max_total_notional=float(c.get("max_total_notional", 30.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 2)),
        )
        self.conn = conn

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
    def _runup(view: MarketView, coin: str) -> float | None:
        """Current return vs the listing reference price (None if unavailable)."""
        info = (view.extra.get("new_listings", {}) or {}).get(coin)
        mid = view.mids.get(coin)
        if not info or not mid or mid <= 0:
            return None
        ref = info.get("ref_px") or 0.0
        if ref <= 0:
            return None
        return mid / ref - 1.0

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        nl: dict[str, dict] = view.extra.get("new_listings", {}) or {}
        open_pos = self._open_positions()

        # ---- exits FIRST ----
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            entry = pos["entry_px"]
            is_long = pos["side"] == "B"
            adverse = (entry - mid) / entry if is_long else (mid - entry) / entry
            held_bars = (time.time() - pos["ts_ms"] / 1000) / self.cfg.bar_seconds
            runup = self._runup(view, coin)
            reason = None
            if adverse >= self.cfg.stop_pct:
                reason = f"STOP {adverse*100:+.2f}%"
            elif runup is not None and abs(runup) <= self.cfg.exit_runup:
                reason = f"REVERTED runup={runup*100:+.1f}%"
            elif held_bars >= self.cfg.max_hold_bars:
                reason = f"MAX-HOLD {held_bars:.1f}bars"
            if reason:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin,
                    sz=pos["sz"], px=mid, cloid=make_cloid(self.name),
                    reasoning=f"NEWLIST EXIT {coin}: {reason}",
                    market_snapshot={"exit_px": mid, "entry": entry,
                                     "adverse": adverse, "runup": runup},
                ))

        # ---- entries ----
        active = set(open_pos.keys())
        flattening = {d.coin for d in out if d.action == "flatten"}
        active_after = active - flattening
        room = self.cfg.max_concurrent_positions - len(active_after)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        candidates: list[tuple[str, float, float, float]] = []
        for coin, info in nl.items():
            if coin in active_after or coin in flattening:
                continue
            if int(info.get("age_bars", 0)) > self.cfg.max_age_bars:
                continue  # past day 1 — only fresh listings
            if float(info.get("vol_usd", 0.0)) < self.cfg.min_listing_vol_usd:
                continue  # illiquid micro-listing
            mid = view.mids.get(coin)
            if not mid or mid <= 0:
                continue
            runup = self._runup(view, coin)
            if runup is None or abs(runup) < self.cfg.min_runup:
                continue
            candidates.append((coin, runup, float(info.get("vol_usd", 0.0)), mid))
        # fade the biggest overshoot first
        candidates.sort(key=lambda r: abs(r[1]), reverse=True)

        for coin, runup, _vol, mid in candidates:
            if room <= 0 or room_notional < 5.0:
                break
            notional = min(self.cfg.max_notional_per_trade, room_notional)
            if notional < 5.0:
                break
            # popped up (runup>0) -> SHORT (A); dumped (runup<0) -> LONG (B).
            side = "A" if runup > 0 else "B"
            direction = "short" if side == "A" else "long"
            sz = round(notional / mid, 5)
            out.append(Decision(
                agent=self.name, action="place", coin=coin, side=side,
                sz=sz, px=mid, cloid=make_cloid(self.name),
                reasoning=(
                    f"NEWLIST {direction} {coin} runup={runup*100:+.0f}% from listing "
                    f"@ ${mid:.4f} notional ${notional:.2f}"
                ),
                market_snapshot={"mid": mid, "runup": runup, "notional": notional},
            ))
            room -= 1
            room_notional -= notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no day-1 overshoots (|runup|>={self.cfg.min_runup*100:.0f}% "
                    f"& age<={self.cfg.max_age_bars}bars); {len(nl)} new listings, "
                    f"{len(active)} held"
                ),
                market_snapshot={"n_new_listings": len(nl), "n_held": len(active)},
            ))
        return out
