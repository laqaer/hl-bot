"""Hyperliquid vault evaluation — the Path-C capital-formation spike (B16).

Running a Hyperliquid *vault* (a public, copy-tradeable book that outside
depositors can fund) is one of the two capital paths in ``docs/ROADMAP_TO_1M.md``:
it scales the same engine with other people's money once a credible track record
exists. This module is the **spike**: what a vault requires, how its economics
work, what risks the operator takes on, and — the firm part — the gate that says
*do not open a vault until a strategy clears G3*.

Honesty contract (mirrors the rest of this repo's Path-C reports):

* The **gate** is computed, not asserted: a vault is warranted only when a
  durable, net-of-cost edge exists (G3). The edge search is exhausted with NO
  such edge (see ``reports/edge_search``), so the gate currently returns
  NOT READY. This is the load-bearing conclusion and it is derived, not guessed.
* The **mechanics** (leader skin-in-the-game, profit share, depositor lockup,
  capacity) are Hyperliquid protocol facts external to this repo. They are
  recorded as the operator's *current understanding* with ``verified=False`` —
  every one MUST be re-confirmed against the live Hyperliquid docs/contracts
  before any capital decision. Flagging them unverified keeps a publishable
  artifact from laundering a half-remembered number into a fact.

Pure data + rendering: no network, no DB. ``vault_ready`` takes the one input
that actually moves the gate (whether a strategy has cleared G3).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# The roadmap gate a vault must clear before it can open. Transcribed from
# docs/ROADMAP_TO_1M.md: G3 is the durable, net-of-cost-edge live track record.
VAULT_GATE = "G3 — a strategy with durable positive net-of-cost edge and a clean live track record"


@dataclass(frozen=True)
class VaultAspect:
    """One thing to evaluate about running an HL vault.

    ``verified`` is False for any claim sourced from Hyperliquid's protocol docs
    rather than this repo — those must be re-confirmed before a capital decision.
    """

    aspect: str
    understanding: str  # the operator's current (to-be-verified) understanding
    why_it_matters: str
    verified: bool


# Requirements / economics / risks of running an HL vault. Every protocol fact is
# verified=False on purpose: confirm each against the live HL docs before acting.
ASPECTS: tuple[VaultAspect, ...] = (
    VaultAspect(
        aspect="Leader skin-in-the-game",
        understanding="A vault leader is required to keep a minimum share of the "
        "vault's equity in the vault at all times (commonly cited as ~5%). Confirm "
        "the exact current minimum and whether it is enforced on deposit/withdraw.",
        why_it_matters="Sets the operator's own capital floor and aligns the "
        "leader with depositors; it is a hard prerequisite, not a choice.",
        verified=False,
    ),
    VaultAspect(
        aspect="Profit share / fee",
        understanding="The leader takes a profit share of depositor gains (commonly "
        "cited as up to ~10%), typically subject to a high-water mark so losses must "
        "be recovered before fees resume. Confirm the rate, the HWM mechanics, and "
        "whether any management fee exists.",
        why_it_matters="This is the vault's revenue line — the AUM business case "
        "(Path C) depends on it, and an HWM means a drawdown directly defers income.",
        verified=False,
    ),
    VaultAspect(
        aspect="Depositor lockup / withdrawals",
        understanding="Depositor funds are subject to a lockup before withdrawal "
        "(a lock period is documented). Confirm the current lock duration and the "
        "withdrawal cadence/queue mechanics.",
        why_it_matters="Drives depositor expectations and the operator's liquidity "
        "obligations; a mismatch here is a reputational and redemption risk.",
        verified=False,
    ),
    VaultAspect(
        aspect="Capacity & strategy fit",
        understanding="A vault trades one shared book; per-coin/portfolio notional "
        "scales with TVL. Confirm how vault size interacts with HL's margin and any "
        "position limits, and whether the strategy's edge survives at vault scale.",
        why_it_matters="An edge that only clears costs at small size (the maker-only "
        "leads in the edge search) may decay as TVL grows — capacity is part of the edge.",
        verified=False,
    ),
    VaultAspect(
        aspect="Operational / smart-contract risk",
        understanding="Running a vault exposes the operator to HL protocol/contract "
        "risk, key-management for a larger book, and the duty to operate continuously. "
        "Confirm the custody model and the failure/halt behaviour.",
        why_it_matters="Other people's money raises the bar on the safety chassis and "
        "uptime; an outage or key compromise is now a fiduciary failure, not a self-loss.",
        verified=False,
    ),
    VaultAspect(
        aspect="Regulatory / disclosure",
        understanding="Soliciting deposits may carry jurisdiction-specific obligations. "
        "Confirm what disclosures HL requires and what the operator independently owes "
        "depositors before accepting outside capital.",
        why_it_matters="A track record is necessary but not sufficient; the legal "
        "wrapper gates whether outside capital can be accepted at all.",
        verified=False,
    ),
)


def vault_ready(*, g3_cleared: bool) -> dict[str, Any]:
    """Compute the vault-open gate.

    The only input that moves the gate is whether a strategy has cleared G3 (a
    durable net-of-cost edge with a live track record). Everything else (the
    mechanics above) is preparation that is moot until the gate is open.
    """
    if not g3_cleared:
        return {
            "ready": False,
            "gate": VAULT_GATE,
            "reason": "No strategy has cleared G3: the edge search is exhausted with "
            "no durable net-of-cost edge (see reports/edge_search). Opening a vault "
            "now would solicit deposits into a book with no demonstrated edge — "
            "forbidden by the evidence-before-capital rule.",
        }
    return {
        "ready": True,
        "gate": VAULT_GATE,
        "reason": "A strategy has cleared G3. The mechanics below must each be "
        "verified against live Hyperliquid docs before opening the vault.",
    }


def build_vault_evaluation(*, g3_cleared: bool = False) -> dict[str, Any]:
    """Assemble the full vault-evaluation record."""
    return {
        "headline": "Evaluation spike for running a Hyperliquid vault (Path C). The "
        "gate is G3 — a durable net-of-cost edge with a live track record — which is "
        "NOT met (the edge search found no edge). The protocol mechanics below are the "
        "operator's current understanding and MUST be re-verified against live HL docs.",
        "gate": vault_ready(g3_cleared=g3_cleared),
        "n_aspects": len(ASPECTS),
        "n_unverified": sum(1 for a in ASPECTS if not a.verified),
        "aspects": [asdict(a) for a in ASPECTS],
    }


def to_markdown(record: dict[str, Any]) -> str:
    g = record["gate"]
    verdict = "READY" if g["ready"] else "NOT READY"
    lines = [
        "# hl-bot — Hyperliquid vault evaluation (B16)",
        "",
        record["headline"],
        "",
        f"## Gate: {verdict}",
        f"**Requires:** {g['gate']}",
        "",
        g["reason"],
        "",
        f"## Aspects to evaluate ({record['n_unverified']} of {record['n_aspects']} "
        "unverified — confirm against live HL docs)",
        "| aspect | current understanding | why it matters | verified |",
        "|---|---|---|:--:|",
    ]
    for a in record["aspects"]:
        mark = "yes" if a["verified"] else "**NO**"
        lines.append(
            f"| {a['aspect']} | {a['understanding']} | {a['why_it_matters']} | {mark} |"
        )
    lines += [
        "",
        "*This is a spike, not a solicitation. Protocol mechanics are unverified until "
        "confirmed against live Hyperliquid documentation, and no vault opens until the "
        "G3 gate is met. Spec only — nothing here touches capital.*",
    ]
    return "\n".join(lines)


def export(out_dir: str | Path, *, g3_cleared: bool = False) -> tuple[Path, Path]:
    """Write vault_evaluation.{json,md}; return their paths."""
    record = build_vault_evaluation(g3_cleared=g3_cleared)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "vault_evaluation.json"
    mp = d / "vault_evaluation.md"
    jp.write_text(json.dumps(record, indent=2))
    mp.write_text(to_markdown(record))
    return jp, mp
