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
- Entry rule: predicted next-hours funding annualized ≥ threshold (sweep:
  15%/25%/40% APR) sustained over a lookback (sweep: 8h/24h/72h mean), and
  spot book depth at touch ≥ 4x our clip.
- Exit rule: funding mean over same lookback < exit band (sweep: 5%/10% APR),
  or spot−perp price gap exceeds a stop (basis blowout guard, e.g. 50bps),
  or max-hold 14d.
- Expected holding period: days–weeks (regime length of funding episodes).
- Expected trade frequency: 0.05–0.3 round-trips per coin per day — low churn
  is the point.

## Cost sensitivity (the kill question)
- Gross edge: the funding rate itself. 20% APR ≈ 5.5 bps/day held.
- Execution: maker-first both legs (4 fills per round trip: spot in/out,
  perp in/out).
- Net edge at 1.5bp maker × 4 legs = 6bps RT: positive after ~1.1 days held
  at 20% APR; a 10-day hold nets ≈ 49bps. PASS.
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
- Sweep grid: enter_apr × lookback × exit_apr × universe (HYPE + the 3 most
  liquid spot pairs); configs/sweeps/spot_perp_carry.yaml.
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
