# Alpha roadmap — the honest plan

Written 2026-06-12 (post-overhaul review). This is the working plan for growing
the book. It is owned by the operator, executed by the autonomous loop within
its boundaries (`ralph/PROMPT.md`), and revised monthly against evidence.

## 0. The goal, with honest math

Target stated: **$1M by year end**. ~6.5 months remain. What that requires,
compounded, by starting capital:

| Start    | Multiple | Per month | Per day (compounded) |
|----------|----------|-----------|----------------------|
| $1k      | 1000x    | 2.9x      | +3.6%                |
| $10k     | 100x     | 2.0x      | +2.4%                |
| $100k    | 10x      | 1.4x      | +1.2%                |

No durable strategy class returns 2-4% **per day**. Anyone claiming it is
describing leverage + luck, i.e. a distribution with most of its mass at zero.
A genuinely excellent outcome for a small, well-run quant book is **5–15% per
month** — small capital is an *advantage* (more edge per dollar of capacity),
but not a 100x-in-six-months advantage.

So the plan does NOT pretend trading PnL alone reaches $1M this year. It
maximizes the three things that compound toward it, in order of controllability:

1. **Verified edge, compounded.** Confirm the carry class on real history,
   run it at capacity, add the new strategies below. Realistic target:
   2–5x on trading capital by year end. Every gate stays evidence-based.
2. **Bounded convexity.** The moonshot sleeve (separate sub-account, hard
   cap, defined max loss) takes the high-variance shots — liquidation
   cascades, new-listing dislocations — where a 10–50x sleeve outcome is
   *possible* without risking the core book.
3. **Capital formation (the actual 100x lever).** A 6-month verified track
   record (`hlbot track-record`, G3) unlocks the HL vault path: leader earns
   10% of depositor profit. AUM, not returns, is how small books become big
   ones. Target: vault-ready record by Q4. $1M *managed* is a far more
   plausible year-end state than $1M *owned* — and it pays toward the same end.

Monthly checkpoint (operator + loop): actual vs. target compounding, kill or
scale strategies by evidence, revisit capital strategy.

## 0.5. The funding-regime finding (2026-06-13, real 180d data)

First-ever confirm + sweep on real history produced **0 trades** for both carry
agents — and the diagnosis is the most important empirical result we have:

**Hyperliquid funding is baseline-dominated.** Over 180d the median funding for
nearly every coin (BTC, HYPE, DOGE, WIF, kPEPE) is pinned at **~11% APR** — the
exchange's interest-rate baseline (≈0.01%/8h) — escaping it only during rare
directional spikes (WIF→112%, HYPE→93%, kPEPE→79% APR, a handful of hours each).
The old thresholds (≈88% APR entry) sat at the p99.9 of what occurs, so the
strategies were eligible to trade ~3 hours in 4,321. Not unprofitable — starved.
(Volume gate exonerated: BTC shows $13B/day; the day_ntl_vlm fix is correct.)

Consequences (these reshape the strategy priority):

1. **Cross-sectional carry (xfund) is structurally weak here.** With every coin
   pinned at the same 11% baseline, there is no persistent dispersion to harvest
   — shorting one at 11% and longing another at 11% nets ~0 before 4 legs of
   fees. Dispersion exists only in the rare spikes. **Demoted from flagship.**
2. **The 11% baseline IS the harvestable prize — via spot↔perp (S4).** Long spot
   + short perp collects the ever-present baseline continuously, market-neutral,
   no spike required; ~6bps of round-trip cost amortized over a multi-week hold
   is trivial against 11%/yr. The data proves the baseline is rock-steady. **S4
   is now the #1 EV build, not just a nice-to-have.**
3. **Single-name spike capture (funding_carry) is the cheap interim test.** Short
   the one coin whose funding spikes above baseline; recalibrated to enter at
   ~26% APR (0.00003/hr), exit near baseline. A few high-value, negative-skew
   trades — re-sweeping to see if they beat costs. Live candidate or clean kill.

## 1. Verdict discipline: is there alpha today?

Nothing is "alpha" until `hlbot confirm` passes on real history **and** paper
matches live within tolerance. Status at writing:

| Strategy           | Class                      | 2026-06-12 audit verdict |
|--------------------|----------------------------|--------------------------|
| xfund_carry_v1     | x-coin funding carry       | **FIX → flagship.** Fixed: dead hysteresis (rotation churn), no stops, de facto short-only book. Quant estimate after fixes: +3–8 bps/day on deployed notional, Sharpe ~0.8–1.5. G0 on real data still pending (host). |
| liq_cascade_v1     | post-cascade dislocation   | **REBUILD.** Feed was dead code (V1) — it has never seen an event; effect is real and capacity-advantaged at our size. Rebuilt as maker-resting reversion (V2/E5): +2–5 bps/day averaged, episodic. |
| funding_carry_v1   | single-name carry          | **FIX, keep small.** −5 to +5 bps/day, negative skew (shorts the strongest momentum). Stops/re-entry bug fixed; satellite only. |
| twap_mr_regime_v1  | 1h mean reversion + regime | **FROZEN (paper).** Backtest window (60h) ≠ live window (1h) — unified, needs re-confirmation (V8). ~0 expectancy expected. |
| femr_v1            | funding-extreme reversion  | **RETIRED.** −51 bps/7d measured live; paper evidence was structurally broken; superseded by funding_carry. |
| twap_mr_v1, basis_v1 | —                        | retired (confirmed post-cost bleeders) |

The audit also found the evidence machine itself was optimistic exactly where
the thesis lives (backtest maker fills at mid with 100% probability, exits
priced maker when live exits are taker, G0 with no minimum trade count, sweep
selection consuming the OOS window, promotion gates passable by zero-edge
strategies via rolling-window repeated looks). All fixed on the PR branch —
**every prior backtest/sweep number is invalidated; re-run after merge.**

First host action remains `docs/GO_LIVE.md` step 2 (backtest-fetch + confirm
--record). Until then every claim about alpha is a hypothesis.

## 2. New edges to build (priority order)

### S4 — HL spot↔perp basis carry (cash-and-carry) — HIGHEST PRIORITY
Long HL spot, short the same coin's HL perp when funding is positive: collects
funding with **zero cross-coin basis risk** (xfund's main residual) on a single
venue, no withdrawal latency. Universe: coins with HL spot pairs (HYPE, PURR,
…). Capacity: limited by spot book depth — fine at our size. Costs: 2 legs
each way; design for maker entries. Expected net: the funding rate itself
minus ~6–10bps round trip amortized over the hold — on sustained 20–60%/yr
funding regimes this is the cleanest carry available to us.
Spec: `docs/research/S4_spot_perp_carry.md` (engine: spot fills + perp
ownership already exist; needs spot order support in exec + a spot/perp pair
agent + backtest over fetched spot candles).

### S5 — Cross-venue funding signal (then arb)
Binance/Bybit publish funding rates free, no auth. Phase 1 (signal only, no
second account): use the cross-venue funding spread as an entry filter and
fair-value anchor for HL carry — HL funding far above Binance's for the same
coin is a stronger, mean-reverting carry signal than HL funding alone.
Phase 2 (true arb, needs a second venue account + capital split): short the
rich-funding venue / long the cheap one. Phase 1 is pure data plumbing on the
host (`research/funding_xvenue.py`, nightly fetch + features into the carry
agents' MarketView extra).

### E5 — Event-driven liquidation reactor
The cascade edge decays in seconds; a polling cycle cannot catch bottoms.
Move liq_cascade triggering into the WS process: on a qualifying cascade
(notional threshold from the accruing liq_log calibration, S2), fire a
single-agent mini-cycle immediately (same guardrails, same kill checks —
*speed of reaction, not relaxation of gates*). This is our only genuinely
latency-sensitive edge; everything else (carry) is indifferent to speed, so
infra effort goes here and nowhere else.

### S6 — Funding-hour positioning ("settlement sniping")
HL funding accrues hourly from a premium TWAP — largely knowable minutes
ahead. Enter shortly before settlement on extreme prints, exit after.
Small capacity, real at our size, pairs naturally with femr. Research first:
measure persistence of the pre-settlement premium on fetched history.

### S7 — New-listing playbook (moonshot sleeve)
New HL perp listings have recurring day-1/week-1 dynamics (no funding
history, thin books, forced discovery). Sleeve-only, hard-capped. Research
doc from listings history before any order.

### Explicitly NOT doing
- Cross-exchange *price* arbitrage / HFT market-making vs. colocated firms —
  we lose the latency war; funding/basis arbs are the slow games we can win.
- Anything that needs the funded key off the host or unsandboxed withdrawals.

## 3. Execution & signals: what speed actually buys us

| Loop                  | Cadence today | Needs |
|-----------------------|---------------|-------|
| Carry entries/exits   | minutes — fine | better fills, not speed: maker lifecycle telemetry (E1) → tune reprice/timeouts (E2) |
| Maker requote         | engine cycle  | 5s cycle is enough once E1 confirms fill rates |
| liq_cascade           | engine cycle — too slow | E5 event reactor (sub-second from WS event to order) |
| Fill detection        | REST ingest (minutes) | E3 userFills WS subscription → instant ownership + exits armed |

Signal inventory to wire into MarketView (all free, host-fetchable): OI,
volume profile, liq flow (have), cross-venue funding (S5), spot flows once S4
lands, listings calendar (S7). Each enters as a *filter* on existing agents
first (cheap to test), a standalone agent only after research shows
standalone edge.

## 4. Management & cadence (who does what)

- **Autonomous loop (ralph):** executes this roadmap's specs in priority
  order within `ralph/PROMPT.md` boundaries (may not touch gates, caps,
  agent_state, keys). Nightly sweeps keep parameter surfaces fresh.
- **Claude sessions (me):** PR review/merge babysitting, deep reviews like
  this one, spec writing, monthly plan revision against the scorecards.
- **Human (only two jobs):** run the host sequence (GO_LIVE steps 0–4 — the
  network-blocked, key-touching steps only a host can do), and the monthly
  capital decision (deposit size, moonshot cap, vault timing).

Weekly review artifact: `hlbot report` + `hlbot track-record` numbers pasted
into PROGRESS.md by the loop; monthly: this doc's §0 checkpoint updated.

## 5. Current bottleneck

Everything above is gated on one fact nobody has ever measured: **does the
carry class clear costs on real Hyperliquid history?** Run GO_LIVE step 2 on
the host. If yes — scale per the gates and build S4/S5/E5 in parallel. If
no — the research pipeline (S4 first, it's a different trade) is the path,
and no capital goes live on hope.
