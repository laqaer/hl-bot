"""Moonshot-sleeve ring-fence invariants as code (CAPITAL.md Track D, B17).

The sleeve is the deliberately negative-EV, fat-right-tail corner of the
capital plan: a tiny, separately-funded account for convex bets the core
book must never take. The single engineering property that makes it
survivable is the ring-fence — worst case is bounded and written down
*before* any bet exists. docs/MOONSHOT.md is the spec; this module turns its
promises into a mechanical verdict over a real Hyperliquid
``clearinghouseState`` snapshot, so "ring-fenced and loss-bounded" is a
checkable fact rather than a vibe:

- every open position must be **isolated margin** — on HL that is the
  defined-max-loss primitive (loss is capped at the margin posted to the
  position); a cross position silently puts the whole sleeve behind one bet;
- each bet's isolated margin must stay under the **per-bet cap** (a fraction
  of the hard cap), so no single bet can kill the sleeve;
- at most ``max_concurrent_bets`` positions;
- equity at/below the **kill floor** means the sleeve is DEAD: flatten and
  stand down — refunding to chase is the failure mode the sleeve exists to
  contain;
- equity above the **hard cap** is a ratchet event: sweep the excess to core
  (the tail paying off is the only reason the sleeve exists — bank it);
- the sleeve address must not be a core account (trader or vault) — that
  would be the ring-fence not existing.

Pure functions, no network; ``hlbot sleeve-check`` is the read-only CLI.
What code cannot see: external top-ups. The hard cap bounds loss only while
the operator funds the sleeve once per written-down tranche (MOONSHOT.md
"Funding & death"). Nothing here trades; the bot never trades the sleeve.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from .scaling import perp_account_value_from_state


@dataclass(frozen=True)
class SleeveConfig:
    """One sleeve tranche's written-down rules (docs/MOONSHOT.md)."""

    # The tranche funded into the sleeve == the most it can ever lose.
    hard_cap: float
    # Per-bet isolated margin <= max_bet_frac * hard_cap.
    max_bet_frac: float = 0.25
    max_concurrent_bets: int = 2
    # Equity <= kill_floor_frac * hard_cap => sleeve is DEAD (flatten, stand
    # down; see MOONSHOT.md for the refund discipline).
    kill_floor_frac: float = 0.25

    def __post_init__(self) -> None:
        if self.hard_cap <= 0:
            raise ValueError("hard_cap must be > 0")
        if not 0 < self.max_bet_frac <= 1:
            raise ValueError("max_bet_frac must be in (0, 1]")
        if self.max_concurrent_bets < 1:
            raise ValueError("max_concurrent_bets must be >= 1")
        if not 0 <= self.kill_floor_frac < 1:
            raise ValueError("kill_floor_frac must be in [0, 1)")

    @property
    def max_bet_margin(self) -> float:
        return self.max_bet_frac * self.hard_cap

    @property
    def kill_floor(self) -> float:
        return self.kill_floor_frac * self.hard_cap


@dataclass(frozen=True)
class SleeveBet:
    """One open position on the sleeve account, as the ring-fence sees it."""

    coin: str
    szi: float
    margin_used: float  # for isolated margin this IS the bet's max loss
    leverage_type: str | None  # "isolated" | "cross" | None (unparseable)
    leverage: float | None
    position_value: float
    unrealized_pnl: float


def parse_sleeve_positions(st: dict) -> list[SleeveBet]:
    """``clearinghouseState`` → open bets. Unlike the tick-path parse
    (``runtime.positions_from_clearinghouse``) this keeps ``leverage.type``,
    because isolated-vs-cross is the property the sleeve's loss bound stands
    on. Malformed entries are skipped, matching that parse."""
    out: list[SleeveBet] = []
    for ap in st.get("assetPositions", []) or []:
        pos = (ap.get("position") or {}) if isinstance(ap, dict) else {}
        if pos.get("coin") is None:
            continue
        lev = pos.get("leverage") or {}
        with contextlib.suppress(TypeError, ValueError):
            lev_value = lev.get("value")
            out.append(SleeveBet(
                coin=str(pos.get("coin")),
                szi=float(pos.get("szi", 0) or 0),
                margin_used=float(pos.get("marginUsed", 0) or 0),
                leverage_type=(
                    str(lev.get("type")) if lev.get("type") is not None else None),
                leverage=float(lev_value) if lev_value is not None else None,
                position_value=float(pos.get("positionValue", 0) or 0),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0) or 0),
            ))
    return out


@dataclass
class SleeveReport:
    config: SleeveConfig
    equity: float = 0.0
    bets: list[SleeveBet] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    has_data: bool = False
    dead: bool = False

    @property
    def status(self) -> str:
        """DEAD outranks VIOLATIONS: a dead sleeve's only legal moves are
        flatten + stand down, whatever else is wrong with the book."""
        if not self.has_data:
            return "NO_DATA"
        if self.dead:
            return "DEAD"
        if self.violations:
            return "VIOLATIONS"
        return "OK"

    @property
    def committed_margin(self) -> float:
        return sum(b.margin_used for b in self.bets)

    @property
    def kill_headroom(self) -> float:
        return self.equity - self.config.kill_floor

    @property
    def sweep_excess(self) -> float:
        return max(0.0, self.equity - self.config.hard_cap)


def evaluate_sleeve(
    config: SleeveConfig,
    st: dict,
    *,
    address: str = "",
    core_addresses: tuple[str, ...] = (),
) -> SleeveReport:
    """Check one clearinghouse snapshot against the sleeve's ring-fence.

    ``address`` is the sleeve account, ``core_addresses`` the accounts it must
    not be (trader, vault). Every violation is a string a human acts on; the
    checker never trades. Equity is ``marginSummary.accountValue`` (includes
    unrealized PnL — the kill floor must see a drawdown before it is realized).
    """
    report = SleeveReport(config=config)
    if not st or not st.get("marginSummary"):
        return report
    report.has_data = True
    report.equity = perp_account_value_from_state(st)
    report.bets = parse_sleeve_positions(st)

    core = {a.lower() for a in core_addresses if a}
    if address and address.lower() in core:
        report.violations.append(
            f"ring-fence breach: sleeve address {address} IS a core account "
            "(trader/vault) — the sleeve must live on its own wallet")

    for b in report.bets:
        if b.leverage_type != "isolated":
            report.violations.append(
                f"{b.coin}: {b.leverage_type or 'unknown'}-margin position — "
                "loss is not bounded by the bet's posted margin; sleeve bets "
                "must be isolated")
        elif b.margin_used > config.max_bet_margin:
            report.violations.append(
                f"{b.coin}: isolated margin ${b.margin_used:.2f} > per-bet cap "
                f"${config.max_bet_margin:.2f} — withdraw margin / take profit "
                "down to the cap (ratchet)")
    if len(report.bets) > config.max_concurrent_bets:
        report.violations.append(
            f"{len(report.bets)} open bets > max {config.max_concurrent_bets}")

    if report.equity <= config.kill_floor:
        report.dead = True
        report.notes.append(
            f"equity ${report.equity:.2f} <= kill floor "
            f"${config.kill_floor:.2f} — sleeve is DEAD: flatten everything, "
            "stand down; refunding now is chasing (MOONSHOT.md)")
    if report.sweep_excess > 0:
        report.notes.append(
            f"equity ${report.equity:.2f} > hard cap ${config.hard_cap:.2f} — "
            f"sweep ${report.sweep_excess:.2f} to core (ratchet; the tail "
            "paying off is the point — bank it)")
    return report
