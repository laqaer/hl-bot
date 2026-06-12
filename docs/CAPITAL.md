# Capital formation playbook — how the bot gets to real size

The hard truth from the math: **a ~$10k account cannot reach $1M by year-end through
trading** (that needs ~50%/month = gambling). $1M is a *capital* problem. This playbook
is how we raise/earn that capital, and every path is **gated on one asset: a 60–90 day,
on-chain, auditable track record (Sharpe > 1.5, max drawdown < 10%, ~2–5%/mo).**

> Numbers below are from June-2026 research; **verify current terms before relying on
> any specific figure** (especially vault fees and prop-firm rules — these change).

## The arithmetic (why capital is the lever)

Required *average monthly* return to reach $1,000,000 by 2026-12-31 (~6.5 months):

| Starting capital | Monthly return needed | Verdict |
|---:|---:|---|
| $10,000 | ~50%/mo | ❌ gambling / ruin |
| $100,000 | ~12.6%/mo | ⚠️ top-1%, high ruin risk |
| $300,000 | ~6.9%/mo | ⚠️ ambitious |
| $500,000 | ~5.1%/mo | ✅ plausible for a disciplined systematic strategy |
| $900,000 | ~3.9%/mo | ✅ plausible |

Realistic year-end outcome of this plan: **~$350k–$700k under management** if the edge
proves and a vault/funding raises capital. $1M most likely lands H1-2027 — acceptable
since the goal is *durable growth*, not the date.

## Track A — Hyperliquid vault (primary; AUM) ★

A vault lets depositors allocate to your strategy on-chain; you (the leader) earn a
share of profits. Best fit: durable, compounding, auditable, and **our bot can run it**
(API/agent-wallet compatible).

- **Economics (verify):** ~**10% profit share** to the leader; leader must keep **≥5% of
  vault TVL** (skin-in-the-game); depositors have a **~1-day lockup**. There may be a
  one-time creation cost — **confirm in the current HL docs before launching** (a
  researched "~10k USDC to create" figure is unconfirmed and may be wrong).
- **What unlocks deposits:** a public, on-chain record. Reference vaults attracting
  millions in TVL all show months of operation + strong, consistent, low-drawdown
  returns. The leaderboard is how depositors discover you.
- **Steps:** (1) prove edge to G3 on the isolated bot account; (2) `hlbot track-record`
  → publishable artifact; (3) create the vault, seed with personal capital (your ≥5%);
  (4) publish the record; (5) set **`HL_VAULT_ADDRESS`** so the bot signs orders *for
  the vault* and reads its state — pointing `HL_TRADER_ADDRESS` at the vault is NOT
  enough (reads would follow the vault while orders quietly execute on the personal
  account); full checklist in `GO_LIVE.md` §Vault retargeting; (6) grow TVL as the
  public record compounds.

## Track B — Prop / funded account (parallel; fast additive capital)

Trade a firm's capital for a profit split after passing an evaluation. Faster than
raising, but capped and unforgiving.

- **Candidates (verify each):** **Hypernova** and **Propr** — Hyperliquid-native,
  on-chain payouts, API/bot-friendly, up to ~$100–200k; **Velotrade** — full REST/WS
  API, ~90% split, $5k–$200k. (Public launch dates / terms were fresh as of mid-2026.)
- **Reality check:** all require a paid eval (sim account, ~5–10 days); **strict
  drawdown rules** (a single breach = permanent loss of funding); per-account caps
  (~$200k); and a **high failure rate (~87% across prop firms in 6 months)**. Run the
  *same* risk-disciplined strategy the bot already enforces.
- **Use it as:** additive capital ($100–200k) on top of the vault — not the whole plan.
- **Prep:** `PROP_EVAL.md` is the checklist; `hlbot prop-check` replays our real
  equity curve against a firm's rules (day-boundary equity loss, trailing DD)
  before any fee is paid.

## Track C — SMA / friends-&-family (later; once the record is strong)

The on-chain vault record doubles as the SMA pitch. Allocators want **3–6 months
verified, Sharpe > 1.5, max DD < 10%, ~2–5%/mo** before deploying $100–300k. On-chain
auditability is the accelerant vs. a traditional discretionary record.

## Track D — Ring-fenced moonshot sleeve (the literal ~1% shot)

The only way a *small* account reaches $1M fast — by accepting ~1% odds.
- **Separate sub-account**, **hard-capped** (only money you can zero), **defined max
  loss per bet**; asymmetric/convex positions (defined-risk leverage, option-like).
- **Negative expected value, fat right tail.** **Never** touches core/vault capital.
- Spec'd in **`MOONSHOT.md`** (backlog B17): ring-fence invariants (isolated-margin
  only, per-bet cap, kill floor, sweep ratchet, address isolation) + bet/refund
  discipline; `hlbot sleeve-check` verifies the invariants read-only against the
  funded account. Fund only what you can lose, only after live G1+ evidence.

## Sequencing

1. **Now → ~8 weeks:** prove + sharpen the edge on the isolated ~$10k (Track 1 of the
   roadmap). Stop hand-trading. Build the track record.
2. **~Month 2–3 (gated on the record):** open the **vault**, seed it, publish; run **one
   prop eval** in parallel.
3. **Month 3+:** grow vault TVL; pitch an **SMA** if the record holds; keep the
   **moonshot** tiny and isolated.

## Recommended

**Lead with the vault** (durable compounding AUM, fits "grow durably"); **prop eval in
parallel** for speed; **moonshot stays small**. **Do not** sink large personal capital —
use the ~$10k to *prove the edge*, then let raised/funded capital do the lifting.

_See `ROADMAP_TO_1M.md` for the gates (G0–G3) and `GO_LIVE.md` for the live runbook._
