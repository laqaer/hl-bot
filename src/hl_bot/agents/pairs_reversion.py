"""Pairs Reversion — relative-value statistical arbitrage on coin pairs.

Thesis (the seventh, structurally-different signal after six bar-based price- and
funding-theses were pruned by the out-of-time durability bar): two economically
related coins (e.g. ETH/BTC, SOL/AVAX) keep a *stable* price relationship. Their
log-price ratio (the "spread") oscillates around a rolling equilibrium; when it
stretches far from that mean it tends to snap back. So when the spread z-score is
extreme, SHORT the rich leg and LONG the cheap leg in dollar-neutral size and
close as it reverts to zero.

Why this is orthogonal to everything already pruned: momentum (time-series and
cross-sectional) and carry all key off a coin's *own* trailing return or funding
*level*. This keys off the *relationship between two coins* — a pairwise
cointegration/mean-reversion, not relative strength or carry. The book is
market-neutral (each pair is two offsetting legs) so directional beta washes out,
and the signal horizon is hours, so it tolerates the 5-min loop.

Designed for maker execution (confirm with ``--prefer maker``): entries are
patient (post at the touch). Each pair holds until the spread reverts inside the
exit band (``entry_sign * z <= exit_z`` — covers both reversion-to-zero and a
flip clean through the mean), then both legs flatten together.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .base import Agent, MarketView
from .cloid import make_cloid
from .decisions import Decision


def _parse_pairs(raw: Any) -> list[tuple[str, str]]:
    """Accept pairs as a list of (a, b) tuples/lists OR a ``'ETH/BTC|SOL/AVAX'``
    string (``|`` separates pairs, ``/`` separates the two legs), so the universe
    can be overridden from the ``--params`` CLI (which can't pass nested lists).
    Symbols are upper-cased; malformed entries are skipped."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, str):
        items: list[Any] = [p for p in raw.split("|") if p.strip()]
    else:
        items = list(raw or [])
    for it in items:
        if isinstance(it, str):
            parts = [s.strip() for s in it.split("/")]
        else:
            parts = [str(s).strip() for s in it]
        if len(parts) == 2 and parts[0] and parts[1]:
            a, b = parts[0].upper(), parts[1].upper()
            if a != b:
                pairs.append((a, b))
    return pairs


@dataclass
class PairsReversionConfig:
    pairs: list[tuple[str, str]] = field(
        default_factory=lambda: [("ETH", "BTC"), ("SOL", "AVAX"), ("LINK", "AAVE")]
    )
    lookback_bars: int = 48            # rolling window for the spread mean/std (<= closes window)
    entry_z: float = 2.0              # |z| to open a pair
    exit_z: float = 0.5              # close when entry_sign*z falls below this
    min_daily_volume_usd: float = 10_000_000.0
    max_notional_per_trade: float = 25.0   # per leg
    max_total_notional: float = 100.0
    max_concurrent_positions: int = 6


class PairsReversionAgent(Agent):
    def __init__(
        self,
        name: str = "pairs_reversion_v1",
        config: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        super().__init__(name, config)
        c = config or {}
        self.cfg = PairsReversionConfig(
            pairs=_parse_pairs(c["pairs"]) if c.get("pairs") else PairsReversionConfig().pairs,
            lookback_bars=int(c.get("lookback_bars", 48)),
            entry_z=float(c.get("entry_z", 2.0)),
            exit_z=float(c.get("exit_z", 0.5)),
            min_daily_volume_usd=float(c.get("min_daily_volume_usd", 10_000_000.0)),
            max_notional_per_trade=float(c.get("max_notional_per_trade", 25.0)),
            max_total_notional=float(c.get("max_total_notional", 100.0)),
            max_concurrent_positions=int(c.get("max_concurrent_positions", 6)),
        )
        self.conn = conn

    def _open_positions(self) -> dict[str, dict]:
        if self.conn is None:
            return {}
        rows = self.conn.execute(
            """SELECT ts_ms, coin, action, side, sz, px
               FROM agent_decisions
               WHERE agent=? AND coin IS NOT NULL AND action IN ('place','flatten')
               ORDER BY ts_ms ASC""",
            (self.name,),
        ).fetchall()
        open_by_coin: dict[str, dict] = {}
        for r in rows:
            coin = r["coin"]
            if r["action"] == "place":
                open_by_coin[coin] = {"side": r["side"], "sz": float(r["sz"] or 0),
                                      "entry_px": float(r["px"] or 0), "ts_ms": r["ts_ms"]}
            else:
                open_by_coin.pop(coin, None)
        return open_by_coin

    def _spread_z(self, closes_a: list[float], closes_b: list[float]) -> float | None:
        """z-score of the current log-ratio spread vs its trailing ``lookback_bars``
        mean/std. None if either series is too short or the spread is degenerate."""
        lb = self.cfg.lookback_bars
        n = min(len(closes_a), len(closes_b))
        if n < lb + 1:
            return None
        a = closes_a[-(lb + 1):]
        b = closes_b[-(lb + 1):]
        spread: list[float] = []
        for pa, pb in zip(a, b, strict=True):
            if pa <= 0 or pb <= 0:
                return None
            spread.append(math.log(pa) - math.log(pb))
        hist = spread[:-1]                      # exclude the current bar from the baseline
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / len(hist)
        std = math.sqrt(var)
        if std <= 0:
            return None
        return (spread[-1] - mean) / std

    def _eligible(self, view: MarketView, vol: dict[str, float], coin: str) -> bool:
        return (view.mids.get(coin) or 0) > 0 and vol.get(coin, 0) >= self.cfg.min_daily_volume_usd

    def decide(self, view: MarketView) -> list[Decision]:
        out: list[Decision] = []
        closes = view.extra.get("closes", {}) or {}
        vol = view.extra.get("day_ntl_vlm", {}) or {}
        open_pos = self._open_positions()

        # ---- per-pair signal: z and the desired dollar-neutral legs ----
        # desired[coin] = side; pair_of[coin] = (a, b); entry_sign[coin] = +1/-1
        desired: dict[str, str] = {}
        pair_of: dict[str, tuple[str, str]] = {}
        z_of: dict[tuple[str, str], float] = {}
        for a, b in self.cfg.pairs:
            if not (self._eligible(view, vol, a) and self._eligible(view, vol, b)):
                continue
            z = self._spread_z(closes.get(a, []), closes.get(b, []))
            if z is None:
                continue
            z_of[(a, b)] = z
            pair_of[a] = (a, b)
            pair_of[b] = (a, b)
            if z >= self.cfg.entry_z:            # a rich vs b → short a / long b
                desired[a], desired[b] = "A", "B"
            elif z <= -self.cfg.entry_z:          # a cheap vs b → long a / short b
                desired[a], desired[b] = "B", "A"

        # ---- exits: hold each pair until the spread reverts inside the band ----
        flattening: set[str] = set()
        for coin, pos in list(open_pos.items()):
            mid = view.mids.get(coin)
            if mid is None or mid <= 0:
                continue
            pair = pair_of.get(coin)
            z = z_of.get(pair) if pair else None
            if pair is None or z is None:
                continue                          # can't evaluate this pair now → hold
            a, _b = pair
            # entry_sign: the sign of z that this held leg was opened against.
            held = pos["side"]
            if coin == a:
                entry_sign = 1.0 if held == "A" else -1.0
            else:
                entry_sign = -1.0 if held == "A" else 1.0
            if entry_sign * z <= self.cfg.exit_z:
                out.append(Decision(
                    agent=self.name, action="flatten", coin=coin, sz=pos["sz"], px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=f"PAIRS EXIT {coin} ({a}-pair): spread reverted z={z:+.2f}",
                    market_snapshot={"exit_px": mid, "z": z, "pair": f"{pair[0]}/{pair[1]}"},
                ))
                flattening.add(coin)

        # ---- entries: open whole pairs (both legs) into empty slots ----
        active = set(open_pos.keys()) - flattening
        room = self.cfg.max_concurrent_positions - len(active)
        active_notional = sum(
            p["sz"] * (view.mids.get(c) or p["entry_px"])
            for c, p in open_pos.items() if c not in flattening
        )
        room_notional = self.cfg.max_total_notional - active_notional

        placed: set[str] = set()
        for a, b in self.cfg.pairs:
            if a not in desired or b not in desired:
                continue
            if a in active or b in active or a in placed or b in placed:
                continue                          # a leg already open / claimed
            if room < 2 or room_notional < 10.0:
                continue
            mid_a, mid_b = view.mids.get(a), view.mids.get(b)
            if not mid_a or not mid_b:
                continue
            leg_notional = min(self.cfg.max_notional_per_trade, room_notional / 2.0)
            if leg_notional < 5.0:
                continue
            z = z_of[(a, b)]
            for coin, mid in ((a, mid_a), (b, mid_b)):
                side = desired[coin]
                sz = round(leg_notional / mid, 5)
                direction = "short" if side == "A" else "long"
                out.append(Decision(
                    agent=self.name, action="place", coin=coin, side=side, sz=sz, px=mid,
                    cloid=make_cloid(self.name),
                    reasoning=(
                        f"PAIRS ENTER {direction} {coin} ({a}/{b} z={z:+.2f}) @ ${mid:.4f}, "
                        f"notional ${leg_notional:.2f}"
                    ),
                    market_snapshot={"z": z, "mid": mid, "notional": leg_notional,
                                     "pair": f"{a}/{b}", "leg": direction},
                ))
                placed.add(coin)
            room -= 2
            room_notional -= 2 * leg_notional

        if not out:
            out.append(Decision(
                agent=self.name, action="hold",
                reasoning=(
                    f"no pair beyond entry_z={self.cfg.entry_z:.1f} "
                    f"({len([z for z in z_of.values() if abs(z) >= self.cfg.entry_z])} stretched "
                    f"of {len(z_of)} evaluable pairs)"
                ),
                market_snapshot={"z_by_pair": {f"{p[0]}/{p[1]}": round(z, 2)
                                               for p, z in z_of.items()}},
            ))
        return out
