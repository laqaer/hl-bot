# Strategy Spec: crowding_reversal_v1 (S8)

> The operator's "how movement affects sentiment affects price" idea, built as
> a +EV fade rather than a momentum chase. This is what femr_v1 *should* have
> been (femr failed because it was mislabeled carry and never exited, not
> because sentiment-extreme reversion is fake). Core-book candidate, not a
> moonshot — it has a structural reason to work and a hard stop.

## Thesis
Reflexivity: a sharp price move pulls in attention and leverage (rising OI,
funding pushed to an extreme, social volume spiking) until the marginal buyer
is exhausted — everyone who was going to chase has chased, the position is
maximally crowded, and the trade unwinds. We are paid to take the other side
of forced, late, over-levered flow. We are the house at the moment of peak
crowding, not a player buying a lottery ticket. The edge is a *risk premium*
for providing liquidity into a crowded unwind, with a defined stop bounding the
"the crowd was right and it keeps running" tail.

This is NOT trend-following and NOT "buy the dip blindly." It fires ONLY at
multi-signal sentiment extremes, and it fades them.

## Signal
Inputs (all host-fetchable, free; wire into MarketView.extra):
- **Open interest** per coin (HL `metaAndAssetCtxs` carries `openInterest`);
  track its 1h/24h change and z-score.
- **Funding** (have) — the crowding gauge: funding far above the ~11% baseline
  (see §0.5 finding) means longs are paying up = crowded long.
- **Price move** — return over a short lookback (1–6h) and its z-score vs the
  coin's own vol.
- **Social volume** (phase 2, optional): LunarCrush / Santiment free-tier
  social-volume z-score, or X (Twitter) mention counts. Phase 1 ships without
  it — OI + funding + price already define crowding; social is a confirmation
  booster, added only if phase-1 evidence justifies the plumbing.

Entry (fade a crowded LONG; symmetric for crowded short):
- price z-score over lookback ≥ `z_enter` (sweep: 2.0 / 2.5 / 3.0), AND
- OI rose with the move (OI z ≥ `oi_z`, sweep: 1.0 / 1.5) — confirms NEW
  leverage drove it, not short-covering, AND
- funding ≥ `funding_extreme` (sweep: 0.00004 / 0.00006/hr ≈ 35 / 53% APR —
  well above the 11% baseline) — the crowd is paying to hold the trade.
- → SHORT the coin (reduce_only-aware), maker entry resting at/above the touch.

Exit:
- take-profit: price reverts `tp_pct` toward the pre-move VWAP (sweep), OR
- funding normalizes back toward baseline (crowding gone), OR
- hard stop `stop_pct` (sweep: 1.5 / 2.5%) — the crowd kept being right, AND
- max-hold `max_hold_h` (sweep: 6 / 12 / 24h) — sentiment edges decay fast.

Holding period: hours. Frequency: a few setups per week universe-wide
(extremes are rare by construction — that's the point).

## Cost sensitivity (the kill question)
- Gross edge estimate: documented crowded-unwind reversions on liquid crypto
  perps run ~30–120 bps over the following 6–24h when the entry is a genuine
  3σ multi-signal extreme; most of the variance is in the tail the stop bounds.
- Execution: maker entry (rest above mid — the crowd's continuation fills you
  at a better price, like liq-cascade), taker exit (stops must cross).
- Net at maker-in / taker-out (~1.5 + 6.5 bps ≈ 8 bps round trip): a 40–120 bps
  reversion clears costs comfortably WHEN it reverts; the kill question is the
  win rate vs the stop, which only real data answers.
- Breakeven: with tp ≈ 60 bps and stop ≈ 200 bps, need win rate > ~77%; with
  tp ≈ 40 and stop ≈ 150, > ~79%. The sweep finds whether any (z, oi, funding,
  tp, stop) cell clears the G0 gate (edge ≥ 3 bps, ≥30 trades, 2× slip robust).

## Risk
- Direction exposure: net SHORT crypto beta while in a position (fading
  pumps) — episodic, not persistent; cap concurrent positions (2–3) and total
  notional. A market-wide melt-up is the worst regime; the stop + max-hold are
  the defense, and the multi-signal gate keeps it out of plain uptrends (an
  orderly trend has price-move WITHOUT extreme funding/OI-spike).
- Negative skew is real (small wins, occasional stop-out) — exactly why the
  stop is mandatory and sizing is small. This is a core-book satellite, not the
  base; the base is S4.
- Portfolio interaction: anti-correlated with momentum/continuation edges;
  complements carry (different driver). Never fade a coin the carry book is
  short for funding reasons without netting — share ownership by coin.

## Validation plan
- Data: need historical OI series (HL `metaAndAssetCtxs` is point-in-time; for
  backtest, either backfill OI from a provider or accrue it forward via the WS
  feed for a few weeks before trusting backtest numbers — note this in the
  build: phase-1 may have to paper-soak for real evidence rather than backtest,
  since OI history isn't in the candle cache). Price/funding are in the cache.
- Sweep grid: z_enter × oi_z × funding_extreme × tp_pct × stop_pct × max_hold,
  on the liquid universe (BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE);
  configs/sweeps/crowding_reversal_v1.yaml.
- G0 expectation: a cell clearing edge ≥ 3 bps net OOS with ≥30 round trips and
  2× slippage robust CONFIRMS the crowded-unwind premium is real and capturable
  at our costs. No such cell → the reversion doesn't beat the stop-loss drag at
  our latency, and S8 is shelved (clean kill, not a slow bleed).

## Promotion ladder proposal
Same shape/minima as the other live agents (CI-pinned floors in
tests/test_gate_minima.py): paper→live_small 5d soak, persist 3d, edge ≥ 3 bps,
n_trades ≥ 20 (round trips), require_g0, 7d paper net ≥ 0; live_small→live 14d,
real fills, n_trades ≥ 10. Tight loss guardrail (it's negative-skew).

## Sources
- Reflexivity (Soros) as the mechanism; crowded-trade unwind literature.
- Crypto-specific: OI+funding+price "quadrant" framework (price↑OI↑ = new
  leverage/continuation until exhaustion; the fade is the late-stage extreme).
- HL `metaAndAssetCtxs` for OI/funding; LunarCrush/Santiment free social tiers.
- The femr_v1 post-mortem (docs/ALPHA_ROADMAP.md): why "sentiment reversion"
  done as undisciplined carry-with-hope failed, and what to fix (explicit
  multi-signal extreme gate + mandatory stop + fast max-hold).
