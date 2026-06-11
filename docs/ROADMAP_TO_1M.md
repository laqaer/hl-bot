# Roadmap to a $1M portfolio by year-end

Honest, numbers-first. Target date **2026-12-31** ≈ **206 days** (~6.8 months)
from 2026-06-08. This is the strategy behind the autonomous loop in
[`../ralph/`](../ralph/) and the prioritized [`../ralph/BACKLOG.md`](../ralph/BACKLOG.md).

> The brief was: *"It's improbable, but find a path forward, even if it's 1%."*
> Here is the unsentimental version, and then the plan that maximizes that 1%.

## 1. The arithmetic decides almost everything

To reach $1,000,000 you need to multiply starting capital `C` by `M = 1e6 / C`.
Compounded over 206 days, the **required average daily return** is `M^(1/206) − 1`:

| Start `C` | Multiple `M` | Daily return needed | ≈ Monthly | Reality check |
|---:|---:|---:|---:|---|
| $100 | 10,000× | **4.6%/day** | ~290%/mo | Pure gambling; ~0% survival |
| $1,000 | 1,000× | **3.4%/day** | ~178%/mo | Gambling; blows up |
| $10,000 | 100× | **2.3%/day** | ~88%/mo | Gambling with extra steps |
| $50,000 | 20× | **1.5%/day** | ~55%/mo | Only via lucky leverage streak |
| $100,000 | 10× | **1.1%/day** | ~40%/mo | Top-0.1% and probably unstable |
| $250,000 | 4× | **0.67%/day** | ~22%/mo | Exceptional but conceivable |
| $500,000 | 2× | **0.34%/day** | ~11%/mo | Hard; top-decile crypto fund |
| $900,000 | 1.11× | **0.05%/day** | ~1.6%/mo | Easy — basically don't lose |

For scale: durable systematic crypto strategies net roughly **1–3%/month** at
size with controlled drawdown. Sustained **10%/month is exceptional**; **40%+/month
is a martingale that eventually gets liquidated.**

**Conclusion:** with a small account, "$1M by year-end" is not an *edge* problem,
it is a **capital-formation** problem. No risk-controlled strategy turns $1k into
$1M in 7 months. Anything that *could* requires leverage whose variance
guarantees ruin long before the finish. So the plan is built around the three
paths that actually carry non-trivial probability.

## 2. Three paths (run all of them; they reinforce)

### Path A — *Earn the right to capital* (the backbone, ~10–30% of the work pays off)
Make the bot a **credible, auditable, risk-controlled return engine**, then feed
it capital. The target return is modest *because the capital does the heavy
lifting*: with ~$700–900k deployed, 1.5–3%/month compounds past $1M; even just
preserving deposited capital through a crypto up-leg gets there.
- The deliverable is a **track record**: ≥60–90 days of positive, costed,
  risk-adjusted edge on real (small) size, produced by the existing supervisor/
  scoring spine so it's trustworthy.
- Capital is added **only as gates are passed** (see §4). This is the rational,
  defensible path and it's where most engineering effort goes.

### Path B — *A ring-fenced moonshot sleeve* (the literal 1% lottery)
The only way a *small* account reaches $1M is a convex bet. Carve a **hard-capped,
fully-loss-bounded** sleeve (e.g. ≤5% of capital, or a fixed amount you can zero)
for asymmetric trades: defined-max-loss leveraged directional, or option-like
payoffs. **Negative expected value, fat right tail.** Sized so it can *never*
touch the core (separate sub-account; the 5×/1× risk caps and guardrails already
make this enforceable). This is the explicit ~1% path — by construction.

### Path C — *Harvest AUM and the ecosystem* (reframes "portfolio" as AUM)
A "$1M portfolio" can be **assets under management**, not just personal capital.
- Run a **Hyperliquid vault**: a real on-chain track record attracts depositors;
  AUM can reach $1M+ even if personal capital didn't. This *requires* Path A's
  track record, so it's free synergy.
- **Ecosystem yield**: HLP/market-maker rebates, points/airdrop farming, referral.
  These are low-variance accelerants, not the main act.

**Recommended mix:** Path A as the spine, Path C as the multiplier on a real track
record, Path B as a small, ring-fenced lottery ticket. Probability of literally
hitting $1M by Dec 31 is still low — but this is how you make it non-zero without
betting the account.

## 3. Engineering plan (what actually moves the needle)

Ordered by leverage. Each item is a backlog epic; details in
[`../ralph/BACKLOG.md`](../ralph/BACKLOG.md).

1. **Find one real edge (offline first).** Now possible: the backtest harness
   (`src/hl_bot/backtest/`) replays real `decide()` over history with costs.
   - Quantify the **taker→maker gap** on every agent (`hlbot backtest --compare`).
     Hypothesis: most of the bleed is the spread.
   - Build **maker (post-only) execution** so the passive strategies stop paying it.
   - Build **`twap_mr_regime_v1`** (wire the existing regime filter) and a
     **maker carry/funding** strategy; keep only what backtests positive *after
     costs* and survives walk-forward.
2. **Make measurement honest.** Fix funding attribution (C4) and per-agent equity
   curves (C5) so the supervisor can promote on real Sharpe/edge, not artifacts.
3. **Match cadence to edge.** WebSocket/event-driven ticks for fast strategies;
   keep the cron only for genuine low-frequency carry (C7).
4. **Scaling machinery (already mostly here).** The 5×/1× portfolio risk rule +
   MetaAllocator means *when* edge + capital exist, size grows safely and
   automatically. Add equity-curve-based position sizing (vol targeting).
5. **Track record + reporting for capital/AUM.** Daily scorecard already exists;
   add a public-grade equity curve, drawdown, and Sharpe export for Path C.
6. **Self-improvement loop.** The Ralph loop (`../ralph/`) works the backlog
   autonomously: research → backtest → propose → human-gate live.

## 4. Gates (capital is deployed only as evidence accrues)

No size increase or capital injection without passing the prior gate. This is the
discipline that keeps Path A from becoming Path B by accident.

| Gate | Evidence required | Unlocks |
|---|---|---|
| **G0 Sim** | A strategy backtests **positive net-of-cost edge** + Sharpe > 1 on ≥90d history, survives walk-forward and a taker-cost stress | Paper deployment |
| **G1 Paper** | ≥30d paper: edge ≥ +5 bps, ≥150 trades, no guardrail breach | `live_small` (existing promotion gate) |
| **G2 Live-small** | ≥30–60d live small: positive net after real fills/funding, max DD < 10% | Scale notional via 5×/1× rule |
| **G3 Track record** | ≥60–90d live: stable Sharpe, controlled DD | Add core capital (Path A) / open vault (Path C) |
| **G-Moon** | Sleeve capped at ≤5% / fixed $, separate sub-account, defined max loss per bet | Path B bets only |

## 5. What we will *not* do

- No unattended **live** trading driven by the autonomous loop. The loop does
  research, code, backtests, and *proposes*; enabling/scaling live capital stays
  human-gated (matches the repo's existing safety posture).
- No raising notional caps to chase losses. `research/strategy_health.py` only
  ever tightens; keep it that way.
- No pretending. If no edge survives costs, the honest output is "this account
  cannot reach $1M by trading; the lever is capital + a small moonshot," and we
  optimize for the best risk-adjusted return per deployed dollar so any capital
  that *does* arrive compounds safely.

## 6. Current status (2026-06-09)

- Backtest harness, confirmation gate (`hlbot confirm`), maker execution, WS feed,
  ops/health, 24/7 AWS deploy, and the self-improvement loop: **all built and live.**
- **Edge: one small but real edge found.** `twap_mr_v1` shows **+29.5 bps, ~7.9
  daily-Sharpe, +$159 over 556 live trades** on the account — genuine, tiny in dollars.
  Carry strategies (`xfund_carry_v1`, `funding_carry_v1`) await real-data confirmation
  (`hlbot confirm`).
- **Deployed:** live on AWS (On-Demand), trading `twap_mr_v1` as a maker, guardrailed,
  self-improving (loop → auto-deploy). The biggest past drag was **manual hand-trading
  (−$8.5k)**, now being stopped + isolated.
- **Capital: the dominant variable.** Plan is ~$10k personal to *prove* the edge, then
  raise via a Hyperliquid **vault (AUM)** + a **prop/funded** account, gated on a
  60–90d track record. Realistic year-end: **~$350k–$700k under management**; $1M most
  likely H1-2027. **See [`CAPITAL.md`](CAPITAL.md) for the full capital playbook.**

