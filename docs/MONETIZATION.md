# Monetization — every revenue lever this system can pull

The strategy review's hard truth stands: the fastest *certain* money is not a
predictive edge, it's (1) **not paying costs you don't have to pay**, (2)
**collecting structural cash flows** (funding carry), and (3) **harvesting the
ecosystem** (airdrop, vault AUM, builder fees) off the back of an auditable
track record. This doc enumerates the levers, their economics, and what in this
repo implements each one. Figures checked 2026-06; re-verify before relying on
them (links at the bottom).

## 1. The fee stack — collapse the cost side first

The book's measured bleed (FEMR −51 bps, TWAP −26 bps) is consistent with a
flat-to-positive signal minus a ~13 bps taker round trip. Every layer below is
multiplicative and requires **no edge at all**:

| Layer | Mechanism | Effect | Status in repo |
|---|---|---|---|
| Maker execution | post-only entries at the touch | ~4.5 bps taker → ~1.5 bps maker per side, no slippage | **Wired** — `--execution auto` routes carry agents maker (`exec/router.py`, `maker_price`) |
| Volume tiers | 14d volume | taker 4.5→2.4 bps, maker 1.5→0 (rebates −0.3 bps at MM tiers) | automatic as volume grows |
| HYPE staking discount | stake ≥10 HYPE (Wood) … 500k+ (Diamond) | **5%–40% off all fees**, no volume cap | operator step — see GO_LIVE checklist |
| Referral discount | sign up via a referral code | **−4%** on first $25M volume | operator step (one-time, account-level) |
| Growth-mode markets | trade HIP-3 growth-mode listings | taker fees cut ~90% (0.45–0.9 bps) | pick coins accordingly in agent configs |

A small account that posts maker, stakes 10 HYPE, and used a referral pays
roughly **1.3 bps maker / ~4 bps taker** instead of 4.5/1.5 — the structural
bleed shrinks by ~70% before any strategy work.

## 2. Funding carry — the structural cash flow

Funding is paid hourly by the crowded side regardless of account size; it's the
one revenue line that doesn't require predicting price. The two agents built
for it are now on the live roster (paper until gated):

- **`xfund_carry_v1`** — market-neutral: short the top-K most-positive-funding
  coins, long the bottom-K; direction cancels, the funding spread remains.
  Highest-conviction strategy in the review; the first G0 candidate.
- **`funding_carry_v1`** — single-name hold-to-collect with a wide stop.

Their funding revenue is now **visible to their own scorecards**
(`scoring/attribution.py`, REVIEW C4 fixed), so the promotion gates evaluate
the real economics.

## 3. Season 2 airdrop — the live, low-variance accelerant

Hyperliquid's Season 2 allocation (38.888% of HYPE supply) is **live now** and
explicitly rewards: perp trading volume, HYPE staking, referrals, HyperEVM
activity — and the guides explicitly call out **delta-neutral strategies** as a
way to farm volume at controlled risk. That is *exactly* what the xfund-carry
book produces as a by-product:

- every maker fill = qualifying volume at ~1.3 bps cost;
- staking for the fee discount (lever 1) double-counts as airdrop weight;
- a dollar-neutral book farms with bounded directional risk.

No code needed beyond what's wired; the posture is: run the carry book live-small
once gated, stake the discount tier, bridge ≥5 HYPE to HyperEVM for the
secondary-season optionality. Treat any airdrop as upside, never as the reason
to oversize.

## 4. Vault AUM — 10% of other people's profits (gated on G3)

A Hyperliquid user vault pays the **leader 10% of depositor profits**. Leader
must keep ≥5% of vault value deposited; creation costs 10k USDC. This converts
a track record into revenue without giving up custody of strategy:

- the deliverable is `hlbot track-record` → `track_record.{json,md,svg,html}` —
  equity curve, Sharpe, drawdown, per-agent attribution, computed from the same
  exchange-reconciled tables the supervisor uses (it cannot flatter live numbers);
- gate: **G3** (≥60–90d live, stable Sharpe, controlled DD). Do not open a vault
  on paper numbers; depositors check the on-chain history anyway.

## 5. Builder codes — fee revenue if this ever fronts other flow

Hyperliquid pays order-flow originators up to **0.1% on perps / 1% on spot** via
builder codes; registering requires only 100 USDC in the perps account, and
users approve a max fee once (`ApproveBuilderFee`). The order path now plumbs
this through (`exec/orders.py::_builder_info`):

```bash
# only when routing flow for users who approved the builder — NEVER for self-flow
HL_BUILDER_ADDRESS=0x...           # the registered builder wallet
HL_BUILDER_FEE_TENTH_BPS=10       # 10 = 1 bp per fill
```

Unset (the default) = no builder field on orders. Enabling it for the bot's own
account would just pay yourself a fee minus the protocol's cut — it exists for a
future where hl-bot executes for other accounts/depositors.

## 6. Referral codes — the other side

Once the account has ≥$10k volume it can generate its own referral code
(rewards on referees' first $1B volume). Zero engineering; worth doing the day
volume qualifies if the track record is being published anyway.

## Priority order (same as the roadmap's honest math)

1. Fee stack + maker routing (done in code; operator finishes staking/referral).
2. Carry book through G0→G1→live-small (the `confirm` gate is the arbiter).
3. Airdrop posture rides along for free.
4. Track record → vault (G3) → builder/referral codes as flow appears.

## Sources

- Fees & tiers: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees>
- Staking tier discounts (5–40%): <https://x.com/HyperliquidX/status/1902304642392072660>
- Builder codes: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes>
- Vaults (10% leader share, 5% min stake): <https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/vaults>
- Referral program (4% discount, $25M cap): <https://hyperliquid.gitbook.io/hyperliquid-docs/referrals>
- Season 2 / HYPE distribution: community guides (verify before acting; terms shift)
