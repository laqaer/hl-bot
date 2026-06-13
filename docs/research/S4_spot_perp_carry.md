# Strategy Spec: spot_perp_carry_v1 (S4)

> **#1 PRIORITY BUILD** — now data-backed, not just plausible. The 2026-06-13
> funding study (docs/ALPHA_ROADMAP.md §0.5) proved HL funding is pinned at a
> steady ~11% APR baseline for nearly every coin, nearly all the time. The
> threshold strategies (xfund/funding_carry) are starved because they wait for
> rare spikes; THIS strategy harvests the baseline that is *always there*.
> Single-venue cash-and-carry: the cleanest, most reliable funding capture
> available to this book, and the foundation of the track-record → vault path.

## Why this is the one (read before building)
The whole carry thesis nearly died on 2026-06-13 when both threshold-based
agents returned 0 confirmable trades. The reason was not "no edge" — it was
that funding rarely leaves its ~11% APR baseline, so any strategy gated on
*extreme* funding almost never trades. Spot-perp carry inverts that: it does
not need a spike. Holding long-spot/short-perp through the baseline collects
~11%/yr market-neutral, continuously, on every coin with a spot pair. At $25–
500 clips the only real questions are (a) execution cost vs. hold length and
(b) spot-leg liquidity — both answered in the validation plan below.

## Thesis
Perp funding is a cash flow paid by the levered crowd to whoever will hold
the hedged other side. Long HL **spot** + short the same coin's HL **perp**
collects positive funding with no cross-coin basis risk (xfund_carry's main
residual) and no cross-venue transfer risk. The position is delta-neutral by
construction; PnL = funding collected − costs − spot/perp basis drift (which
converges by arbitrage on the same venue).

## Signal
- Inputs: HL perp funding (have), HL spot mids + books for spot-listed coins
  (extend MarketView: spot universe from `spotMeta`; mids already partially
  plumbed via `spot_mids`), spot account USDC balance (have via
  spotClearinghouseState).
- Entry rule: funding annualized ≥ a LOW threshold that brackets the ~11% APR
  baseline from BELOW (sweep: **6% / 10% / 14% APR**) sustained over a lookback
  (sweep: 8h/24h/72h mean), and spot book depth at touch ≥ 4x our clip. The
  point is to be IN during ordinary baseline funding, not to wait for spikes —
  the entry only needs to clear the cost-recovery bar (see Cost sensitivity:
  ~6bps RT recouped in ~2 days at the 11% baseline), so the band sits at/below
  baseline, never above it. A spike just makes an already-on position richer.
- Exit rule: funding mean over the lookback drops below the exit band (sweep:
  **0% / 3% APR** — i.e. unwind only when carry no longer pays, not when it
  merely eases), or spot−perp price gap exceeds a stop (basis blowout guard,
  e.g. 50bps), or max-hold 14d.
- Expected holding period: days–weeks (regime length of funding episodes).
- Expected trade frequency: 0.05–0.3 round-trips per coin per day — low churn
  is the point.

## Cost sensitivity (the kill question)
- Gross edge: the funding rate itself. The HL **baseline ~11% APR ≈ 3.0
  bps/day held** is the base case (not a spike) — this is the yield S4
  harvests continuously. 20% APR ≈ 5.5 bps/day.
- Execution: maker-first both legs (4 fills per round trip: spot in/out,
  perp in/out).
- Net edge at 1.5bp maker × 4 legs = 6bps RT: at the **11% baseline** the RT
  cost is recouped in ~2 days, and a 14-day hold nets ≈ 36bps (≈ +2.6 bps/day
  net) — the always-on base case. At 20% APR a 10-day hold nets ≈ 49bps. PASS.
- Net edge at 4.5bp taker + 2bp slip × 4 legs = 26bps RT: needs ~5 days at
  20% APR to break even. Maker fills matter; entry urgency is never justified.
- Breakeven fill rate: with taker fallback only on exits, ≥50% maker entry
  fill rate keeps RT cost under 16bps.

## Risk
- Direction exposure: none by construction (1:1 spot vs perp size; re-true
  on each cycle if legs drift >2%).
- Worst regime: funding flips persistently negative while holding (exit band
  handles); spot illiquidity on exit (depth gate + clip ≤ $250 at start);
  leg-out risk if one leg fills and the other doesn't — REQUIRE perp leg
  fills only after spot leg confirmed, and an unwind rule if the second leg
  is unfilled after N reprices.
- Per-trade max loss: basis stop 50bps + costs ⇒ ~0.6% of clip. Portfolio:
  reduces xfund_carry allocation need; cap combined carry-class notional.

## Validation plan
- Sweep grid: enter_apr (6/10/14%) × lookback (8/24/72h) × exit_apr (0/3%) ×
  universe (HYPE + the 3 most liquid spot pairs);
  configs/sweeps/spot_perp_carry.yaml. NB: every enter band sits at/below the
  11% baseline by design — a grid above baseline would re-create the
  starvation the threshold agents hit (the whole reason S4 exists).
- Data needed: HL spot 1h candles for spot universe (extend backtest-fetch
  with spot candles — `candleSnapshot` works for spot pairs), historical
  funding (have).
- G0 expectation: net ≥ 4bps/day held on 180d with maker costs CONFIRMS;
  < 2bps/day or <60% of days funding-positive REFUTES (means funding
  episodes are too short for the RT cost).

## Promotion ladder proposal
Same shape as xfund_carry_v1 (stricter never looser than gate minima):
paper→live_small: 5d min, edge_bps ≥ 5, n_trades ≥ 40 (count legs),
require_g0. live_small→live: 14d min, net>0, real fills only.

## Implementation notes (for the implementer)
- exec: spot orders go through the same Exchange client (`name` is the spot
  pair); extend place_limit_order's asset resolution + szDecimals cache for
  spot; ownership rows tag `coin` as the spot pair string so reconciliation
  stays per-instrument.
- Leg sequencing lives in the agent (decide returns spot leg first; perp leg
  decision emitted only when spot ownership confirmed) — no executor changes.

## Sources
- HL docs: spot trading + funding mechanics (hourly, premium TWAP).
- Classic cash-and-carry literature; funding-capture write-ups (Ethena
  mechanism = same trade at scale, demonstrating the cash flow is real and
  harvestable; their public yields ≈ what sustained capture looks like).
