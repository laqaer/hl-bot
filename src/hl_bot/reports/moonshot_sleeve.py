"""Moonshot sleeve spec — the ring-fenced Path-B lottery ticket (B17).

``docs/ROADMAP_TO_1M.md`` Path B is the only way a *small* account reaches $1M:
a convex, hard-capped, fully-loss-bounded sleeve for asymmetric bets (negative
expected value, fat right tail) that can **never** touch the core book. This
module is the **spec**: the ring-fence constraints, the load-bearing loss-bound
arithmetic that proves the sleeve cannot bleed the core, and the gate that keeps
it off until it is both correctly configured *and* human-approved.

Honesty contract (mirrors the rest of this repo's Path-C reports):

* Unlike the vault evaluation (B16), every constraint here is **internal** — it
  is derived from this repo's own risk machinery (``risk/scaling.py``,
  ``risk/allocation.py``, ``research/strategy_health.py``) and the roadmap, so
  each carries a real ``source`` rather than an unverified external claim.
* The **loss bound** is computed and invariant: worst-case total sleeve loss
  equals the sleeve budget (``core_capital × sleeve_fraction``), so the core is
  untouched even if every bet zeroes. ``moonshot_sizing`` is that pure arithmetic.
* The **gate** is computed, not asserted: the sleeve activates only when it is
  capped at ≤5%, isolated in a separate sub-account, every bet has a defined max
  loss, **and** a human has approved go-live. Default is NOT READY — flipping the
  sleeve live is human-gated, never automatic.

Pure data + rendering: no network, no DB.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# G-Moon (ROADMAP §4): the sleeve is hard-capped at <=5% of core capital.
SLEEVE_MAX_FRACTION = 0.05

# The gate the sleeve must clear before any Path-B bet. Transcribed from
# docs/ROADMAP_TO_1M.md G-Moon, with the repo's human-gated-live hard rule added.
MOONSHOT_GATE = (
    "G-Moon — sleeve capped at <=5% / fixed $, isolated in a separate sub-account, "
    "a defined max loss per bet, AND explicit human go-live approval"
)


@dataclass(frozen=True)
class SleeveConstraint:
    """One ring-fence rule for the moonshot sleeve.

    ``source`` cites the repo module or roadmap section the rule is derived from —
    these are internal (this repo's risk machinery), not external claims, so they
    are verifiable by reading ``source`` rather than flagged unverified.
    """

    constraint: str
    rule: str
    why_it_matters: str
    source: str


# The ring-fence. Every rule is derived from this repo's risk machinery + roadmap.
CONSTRAINTS: tuple[SleeveConstraint, ...] = (
    SleeveConstraint(
        constraint="Hard size cap",
        rule="The sleeve's total funded equity is <=5% of core capital (or a fixed "
        "dollar amount the operator can zero), set once and never raised to chase a "
        "loss.",
        why_it_matters="Caps the entire Path-B downside at a fraction the core can "
        "absorb; this is what makes a negative-EV bet survivable.",
        source="docs/ROADMAP_TO_1M.md Path B / G-Moon",
    ),
    SleeveConstraint(
        constraint="Separate sub-account",
        rule="The sleeve trades from a sub-account isolated from the core book, with "
        "its own collateral; the 5x-total/1x-position notional caps are computed "
        "against the sleeve's own equity, not the core's.",
        why_it_matters="Isolation means a sleeve blow-up cannot cross-margin or "
        "liquidate the core; the existing cap machinery already enforces this once "
        "it is pointed at the sub-account's portfolio value.",
        source="src/hl_bot/risk/scaling.py (compute_notional_cap)",
    ),
    SleeveConstraint(
        constraint="Defined max loss per bet",
        rule="Every bet pre-commits a max loss; the sum of all concurrent bets' max "
        "losses is <= the sleeve budget, so the sleeve is fully loss-bounded.",
        why_it_matters="Turns 'asymmetric bet' into an auditable number: even if "
        "every bet hits its floor, total loss equals the sleeve budget and no more "
        "(the moonshot_sizing invariant).",
        source="src/hl_bot/risk/allocation.py (resolve_agent_caps, per-trade ceiling)",
    ),
    SleeveConstraint(
        constraint="Tightening-only risk",
        rule="Within the sleeve, automated risk changes may only *reduce* caps; "
        "raising sleeve size or per-bet loss to recover a drawdown is forbidden.",
        why_it_matters="The same discipline that protects the core (health proposals "
        "only ever tighten) keeps a losing sleeve from quietly growing into the core.",
        source="src/hl_bot/research/strategy_health.py (risk-reducing-only) + ralph hard rules",
    ),
    SleeveConstraint(
        constraint="Negative-EV disclosure",
        rule="The sleeve is documented as negative expected value with a fat right "
        "tail — a lottery ticket, not an edge. It is excluded from any track-record "
        "or allocator edge claim.",
        why_it_matters="Conflating a convex lottery with a measured edge would "
        "corrupt the honest Path-C record; the sleeve's purpose is explicitly the "
        "1% tail, sized so it can never bet the account.",
        source="docs/ROADMAP_TO_1M.md Path B ('Negative expected value, fat right tail')",
    ),
    SleeveConstraint(
        constraint="Human-gated activation",
        rule="Funding the sleeve and flipping any bet to live is human-gated; nothing "
        "in the loop may auto-enable a Path-B bet.",
        why_it_matters="A convex, negative-EV bet is exactly the kind of decision the "
        "evidence-before-capital + human-go-live rules exist to keep off the autopilot.",
        source="docs/GO_LIVE.md + ralph hard rules (never enable/scale live trading)",
    ),
)


@dataclass(frozen=True)
class SleeveSizing:
    """The computed, loss-bounded sizing of the sleeve for a given core capital."""

    core_capital: float
    sleeve_fraction: float
    sleeve_budget: float  # core_capital * sleeve_fraction — the entire Path-B downside
    max_bets: int
    per_bet_max_loss: float  # sleeve_budget / max_bets
    worst_case_total_loss: float  # per_bet_max_loss * max_bets == sleeve_budget
    core_floor: float  # core_capital - sleeve_budget: untouched even if the sleeve zeroes


def moonshot_sizing(
    core_capital: float,
    *,
    sleeve_fraction: float = SLEEVE_MAX_FRACTION,
    max_bets: int = 5,
) -> SleeveSizing:
    """Compute the loss-bounded sleeve sizing (the load-bearing arithmetic).

    The invariant this proves: ``worst_case_total_loss == sleeve_budget`` and
    ``core_floor == core_capital - sleeve_budget > 0`` — i.e. even if every bet
    zeroes, the loss is exactly the sleeve budget and the core is untouched.

    Raises ``ValueError`` on inputs that would break the ring-fence (non-positive
    capital, a fraction outside ``(0, SLEEVE_MAX_FRACTION]``, or <1 bet).
    """
    if not core_capital > 0:
        raise ValueError("core_capital must be positive")
    if not 0 < sleeve_fraction <= SLEEVE_MAX_FRACTION:
        raise ValueError(
            f"sleeve_fraction must be in (0, {SLEEVE_MAX_FRACTION}] (G-Moon hard cap)"
        )
    if max_bets < 1:
        raise ValueError("max_bets must be >= 1")

    sleeve_budget = core_capital * sleeve_fraction
    per_bet_max_loss = sleeve_budget / max_bets
    return SleeveSizing(
        core_capital=float(core_capital),
        sleeve_fraction=float(sleeve_fraction),
        sleeve_budget=float(sleeve_budget),
        max_bets=int(max_bets),
        per_bet_max_loss=float(per_bet_max_loss),
        worst_case_total_loss=float(per_bet_max_loss * max_bets),
        core_floor=float(core_capital - sleeve_budget),
    )


def moonshot_gate(
    *,
    sleeve_fraction: float = SLEEVE_MAX_FRACTION,
    separate_subaccount: bool = False,
    per_bet_max_loss_defined: bool = False,
    human_approved: bool = False,
) -> dict[str, Any]:
    """Compute the sleeve-activation gate.

    All of the ring-fence conditions must hold *and* a human must have approved
    go-live. Defaults are the safe state (NOT READY): the sleeve never opens by
    itself.
    """
    unmet: list[str] = []
    if not 0 < sleeve_fraction <= SLEEVE_MAX_FRACTION:
        unmet.append(f"sleeve size exceeds the {SLEEVE_MAX_FRACTION:.0%} hard cap")
    if not separate_subaccount:
        unmet.append("not isolated in a separate sub-account")
    if not per_bet_max_loss_defined:
        unmet.append("per-bet max loss is not defined")
    if not human_approved:
        unmet.append("no human go-live approval (Path-B activation is human-gated)")

    if unmet:
        return {
            "ready": False,
            "gate": MOONSHOT_GATE,
            "unmet": unmet,
            "reason": "The moonshot sleeve must not activate: " + "; ".join(unmet) + ".",
        }
    return {
        "ready": True,
        "gate": MOONSHOT_GATE,
        "unmet": [],
        "reason": "All ring-fence conditions hold and a human has approved go-live; "
        "the sleeve may fund Path-B bets within the computed loss bound.",
    }


def build_moonshot_sleeve(
    *,
    core_capital: float = 10_000.0,
    sleeve_fraction: float = SLEEVE_MAX_FRACTION,
    max_bets: int = 5,
    separate_subaccount: bool = False,
    per_bet_max_loss_defined: bool = False,
    human_approved: bool = False,
) -> dict[str, Any]:
    """Assemble the full moonshot-sleeve spec record.

    ``core_capital`` is illustrative (the loss-bound arithmetic is what matters,
    not the exact dollar figure) and is labelled as such in the headline.
    """
    sizing = moonshot_sizing(
        core_capital, sleeve_fraction=sleeve_fraction, max_bets=max_bets
    )
    return {
        "headline": "Ring-fenced Path-B moonshot sleeve (B17): a hard-capped, "
        "fully-loss-bounded sub-account for negative-EV / fat-right-tail bets that can "
        "never touch the core. The sizing below is illustrative on "
        f"${core_capital:,.0f} core capital; the load-bearing facts are the loss bound "
        "(worst case == the sleeve budget) and the gate (NOT READY until configured and "
        "human-approved). Spec only — nothing here funds or places a bet.",
        "gate": moonshot_gate(
            sleeve_fraction=sleeve_fraction,
            separate_subaccount=separate_subaccount,
            per_bet_max_loss_defined=per_bet_max_loss_defined,
            human_approved=human_approved,
        ),
        "sizing": asdict(sizing),
        "n_constraints": len(CONSTRAINTS),
        "constraints": [asdict(c) for c in CONSTRAINTS],
    }


def to_markdown(record: dict[str, Any]) -> str:
    g = record["gate"]
    verdict = "READY" if g["ready"] else "NOT READY"
    s = record["sizing"]
    lines = [
        "# hl-bot — moonshot sleeve spec (B17)",
        "",
        record["headline"],
        "",
        f"## Gate: {verdict}",
        f"**Requires:** {g['gate']}",
        "",
        g["reason"],
        "",
        "## Loss bound (illustrative)",
        f"- Core capital: ${s['core_capital']:,.2f}",
        f"- Sleeve fraction: {s['sleeve_fraction']:.1%} (hard cap {SLEEVE_MAX_FRACTION:.0%})",
        f"- Sleeve budget: ${s['sleeve_budget']:,.2f}",
        f"- Max bets: {s['max_bets']} → per-bet max loss ${s['per_bet_max_loss']:,.2f}",
        f"- **Worst-case total loss: ${s['worst_case_total_loss']:,.2f}** "
        f"(== sleeve budget; the core floor of ${s['core_floor']:,.2f} is untouched)",
        "",
        f"## Ring-fence constraints ({record['n_constraints']})",
        "| constraint | rule | why it matters | source |",
        "|---|---|---|---|",
    ]
    for c in record["constraints"]:
        lines.append(
            f"| {c['constraint']} | {c['rule']} | {c['why_it_matters']} | {c['source']} |"
        )
    lines += [
        "",
        "*Spec only. The sleeve is negative expected value by design and is excluded "
        "from every track-record/allocator edge claim. It never activates without all "
        "ring-fence conditions met and explicit human go-live approval — nothing here "
        "touches capital.*",
    ]
    return "\n".join(lines)


def export(
    out_dir: str | Path,
    *,
    core_capital: float = 10_000.0,
    sleeve_fraction: float = SLEEVE_MAX_FRACTION,
    max_bets: int = 5,
    separate_subaccount: bool = False,
    per_bet_max_loss_defined: bool = False,
    human_approved: bool = False,
) -> tuple[Path, Path]:
    """Write moonshot_sleeve.{json,md}; return their paths."""
    record = build_moonshot_sleeve(
        core_capital=core_capital,
        sleeve_fraction=sleeve_fraction,
        max_bets=max_bets,
        separate_subaccount=separate_subaccount,
        per_bet_max_loss_defined=per_bet_max_loss_defined,
        human_approved=human_approved,
    )
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "moonshot_sleeve.json"
    mp = d / "moonshot_sleeve.md"
    jp.write_text(json.dumps(record, indent=2))
    mp.write_text(to_markdown(record))
    return jp, mp
