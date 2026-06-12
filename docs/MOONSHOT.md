# Moonshot sleeve — spec (CAPITAL.md Track D, backlog B17)

The ring-fenced, loss-bounded corner of the capital plan: a tiny separate
account for **negative-expected-value, fat-right-tail** bets — the literal
~1% shot at outsized return that the core book must never take. Spec only:
**nothing here is wired into the bot, and funding the sleeve is an
operator-only act.** The bot does not and will not trade the sleeve; if a
"moonshot agent" is ever wanted, it earns its way through the same G0→G3
evidence ladder as everything else, on its own account.

## Why it exists (be honest about this)

Two functions, and the second is the real one:

1. **Convexity exposure.** The core book is deliberately mean-reverting,
   cost-disciplined, drawdown-capped — it structurally excludes lottery
   payoffs. A bounded sleeve is the only sanctioned place for them.
2. **Containment.** The operator is human. Lottery impulses exist; un-spec'd,
   they eventually express themselves *inside the core account* as "just this
   once" position. The sleeve gives the impulse a budget, a fence, and a
   death rule, so the core track record — the asset every capital track in
   CAPITAL.md depends on — stays clean.

Expected value of the sleeve is **negative**. Budget it like entertainment
with a tail, not like an investment. If that framing feels wrong, don't fund
it.

## The ring-fence (the invariants)

Written down at funding time, checked mechanically by `hlbot sleeve-check`
(`risk/sleeve.py` is the rules-as-code):

| Invariant | Default | Why |
|---|---|---|
| **Hard cap** = the tranche funded in | ≤ 1–2% of total capital | The most the sleeve can ever lose. No top-ups within a tranche. |
| **Isolated margin only** | always | On Hyperliquid, isolated margin IS the defined-max-loss primitive: a bet can lose its posted margin and nothing else. A cross position silently puts the whole sleeve behind one bet — checker flags it. |
| **Per-bet margin cap** | ≤ 25% of hard cap | No single bet kills the sleeve; ~4 independent shots per tranche minimum. |
| **Max concurrent bets** | 2 | Correlated alt longs are one bet wearing two coats; keep the count low enough to know what the book is. |
| **Kill floor** | 25% of hard cap | At/below it the sleeve is **DEAD**: flatten everything, stand down. The remaining stub goes back to core. |
| **Sweep ratchet** | equity > hard cap | Excess above the cap is swept to core. The tail paying off is the only reason the sleeve exists — bank it. Same per-bet: margin grown past the per-bet cap gets taken down to the cap. |
| **Address isolation** | always | The sleeve address must equal **none** of `HL_TRADER_ADDRESS` / `HL_ADDRESS` / `HL_VAULT_ADDRESS`. Checker flags a match as a ring-fence breach. |

What code cannot enforce: **top-ups**. The hard cap bounds loss only while
funding stays one written-down tranche. That discipline is the operator's;
the refund rules below are the contract.

## Wiring (operator runbook, when/if funded)

1. **Fresh wallet.** New key, never placed in any bot env file
   (`hl-bot-api-wallet.env`, systemd EnvironmentFile, `.env`). The bot's
   services must not be able to see it, let alone trade it.
2. **Fund one tranche** by explicit transfer. Record in a dated note (date,
   tranche $, the four rule numbers): that note is the hard cap.
3. **Set `HLBOT_SLEEVE_ADDRESS`** in the *operator's* shell env (not the
   bot's) for convenience, and run the checker:
   `hlbot sleeve-check --hard-cap <tranche>` — before and after every bet,
   weekly in between. It is read-only and never trades.
4. **Bets are manual.** Isolated margin, leverage within the coin's HL
   limits, margin within the per-bet cap.

## Bet discipline

- **Pre-register every bet** before entry, in the same dated note: thesis,
  invalidation (what kills the thesis), max loss (= the isolated margin),
  rough target. A bet you can't write down in three lines is not a thesis,
  it's a mood.
- **No averaging down. No margin adds to losers.** The posted margin was the
  defined max loss; feeding a losing bet is redefining it.
- **Exit on thesis death, not on price.** Invalidation hit → close, even if
  "it's about to turn". Target hit → take at least the ratchet.
- **Funding is part of the bet.** A held perp position pays/collects funding
  every hour; a crowded lottery long can bleed double-digit bps/day. Count
  expected funding inside the max-loss budget when sizing the hold horizon.

## Funding & death

- The sleeve is **dead at the kill floor** — flatten, sweep the stub to
  core, stand down **≥ 90 days**.
- A **refund is a fresh decision**, never a reaction: at most one tranche
  per quarter, never in the same week as the death (no chasing), and only
  from realized core profits — the sleeve scales with success, not with
  frustration. This is the sleeve-shaped version of the repo-wide rule:
  *never raise risk to chase losses.*
- Lifetime budget: if two consecutive tranches die, the sleeve concept is
  re-evaluated, not refunded.

## Measurement & honesty

- **Out of the track record by construction.** The sleeve lives on its own
  address; the bot's DB never ingests it, so `hlbot track-record`, the
  scorecards, and the supervisor never see it. A public record polluted by a
  deliberate −EV sleeve would mislead exactly the allocators Tracks A–C are
  courting.
- **In the personal P&L always.** Tranches in, sweeps out, and the running
  net go in the same dated note. The honest expected trajectory of that
  number is *down with occasional spikes*; if the note stops being updated,
  that is the containment failing.

## Gates

| Action | Gate |
|---|---|
| Funding a tranche | Operator-only, after the core book has live G1+ evidence (same bar as a prop eval — the sleeve is junior to proving the edge). |
| Any bot involvement | Out of scope. A future moonshot *agent* starts at G0 like everything else. |
| Loop (Ralph) involvement | Spec + read-only tooling only. The loop never funds, wires, or trades the sleeve. |
