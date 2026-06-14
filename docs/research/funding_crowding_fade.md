# Strategy Spec: funding_crowding_fade_v1 (D2a)

> Built from the "funding-settlement snap" research (BACKLOG D2a). The research
> found the edge is **not** about the hourly settlement clock — it's a *crowding*
> fade gated by funding. This is the OI-free, fundable subset of S8
> (`docs/research/S8_crowding_reversal.md`): the multi-signal crowding gate
> collapses to the two signals HL actually gives us without an OI history feed —
> **funding extremity + a vol-normalized price overshoot**.

## Thesis
When a coin's funding sits well above the ~11% APR baseline (§0.5 of
ALPHA_ROADMAP), longs are paying up to hold — the trade is crowded — and price
has usually overshot in that direction. We fade the overshoot and collect the
reversion over the next ~30–60min. We are paid a risk premium for taking the
other side of crowded, late, over-levered flow, with a hard stop bounding the
"the crowd kept being right" tail. Symmetric for crowded shorts (funding far
below baseline / negative).

Distinct from `dislocation_reversion_v1`, which fades sharp 5m overshoots at
|z|≥3 with **no** funding gate. Here the funding gate supplies the selectivity,
so a much **milder** overshoot (|z|~1) is tradeable. In the research only ~1% of
these entries reach |z|≥3, so the two edges barely overlap — this is additive.

## Signal (host-fetchable, free; identical units in backtest and live)
- **Funding** — `view.extra['funding_hourly'][coin]`, the unscaled 1h rate
  (NOT `view.funding`, which is the per-bar rate = hourly/12 in a 5m backtest).
  Gate: `|funding APR| ≥ funding_min_apr` (sweep: 15 / 20 / 30%).
- **Overshoot** — z = (mid − vwap)/sigma over the 5m/5h basis
  (`view.extra['candles_5m']`), same as dislocation/twap_mr. Gate: `|z| ≥
  z_enter` (sweep: 1.0 / 1.5 / 2.0) AND `sign(z) == sign(funding)`.

Entry (fade a crowded LONG; symmetric for short): funding ≥ +gate AND z ≥
+z_enter → **SHORT** (taker — a reversion must get in now; resting maker has
adverse selection). Exit: z reverts to |z|≤z_exit (take profit), or z crosses,
or hard stop `stop_pct` (sweep includes 2%), or `max_hold_bars` (6 / 12 ≈
30 / 60min). Perp-only, conservative caps ($25/trade, $75 total, 3 concurrent).

## Empirical findings (research, ~17.5d of 5m + paged funding, 8 coins)
Event study of 2919 hourly settlements first, then a net-of-cost fade sim
(6.5bps taker round trip) firing on any 5m bar:
- Baseline funding (the ~89% of hours pinned at 11%) shows **no** edge — exactly
  as the §0.5 finding predicts. The edge needs funding meaningfully above
  baseline: the 10–20% APR band is net-NEGATIVE; ≥15% is where it turns on.
- fmin=15, zmin=1.0, hold=12 (60min), stop=2%: **+38bps net / 65% win, ~850
  trades**; the edge rises with zmin and hold and is robust to stop level (1–3%).
- Settlement-timed firing (top of hour only) gives the same per-trade edge at
  ~1/10th the trades — confirming the clock is irrelevant; crowding is the driver.
- Concentration: strongest in meme coins (WIF/kPEPE); SOL/HYPE modestly positive;
  BTC rarely qualifies (its funding stays at baseline). XRP ~flat.

## Cost & data
- Net of a 6.5bps taker round trip the gross reversion clears comfortably WHEN it
  reverts; the kill question is win-rate vs the stop, which only the G0 gate
  (walk-forward + 2× slip + min trades) answers.
- **Data fix shipped with this work:** HL `fundingHistory` is capped at the
  OLDEST ~500 rows per request, so a wide-window fetch returned only 69–90d-old
  funding — fatal for a funding-as-signal agent. `fetch_funding_history_window`
  pages forward to cover the recent window (cache v4). `funding_hourly` is now
  plumbed through `Frame`/`MarketView.extra` so the signal has identical units in
  backtest and live (the twap_mr window-mismatch lesson).

## G0 VERDICT (2026-06-14): NOT CONFIRMED — do not promote
Sweep `configs/sweeps/funding_crowding_fade_v1.yaml` (funding_min_apr × z_enter ×
max_hold_bars × stop_pct, 4- and 8-coin universes), ranked by IN-SAMPLE, ✅ only
if OOS also clears (edge ≥ 3bps net, ≥30 IS / ≥10 OOS trades, 2× slip robust).

**0 / 36 combos confirmed** (`research/results/2026-06-14_funding_crowding_fade_v1.md`):
- In-sample is strong and robust through 3× slippage (single confirm at defaults:
  IS +15.0bps / sharpe +10; full-sample cost ladder +12.4 → +5.9bps maker→taker-3×).
- But the **walk-forward OOS fails**: the in-sample-best combo has a positive OOS
  on just **1 trade** (< the ≥10 floor), and every combo with enough OOS trades
  (10–18) is OOS-NEGATIVE (−2.5 to −17.3bps). The recent ~5d holdout is adverse.

Why exploratory sims looked better: this thesis was +38bps in a hand-rolled
fixed-hold sim over the full ~17.5d, but (a) the G0 walk-forward tests only the
recent ~5d (≈15 trades — high variance), (b) the agent's z-exit/z-cross/cap logic
realizes a different trade set than a fixed-hold sim, and (c) the data window
rolls daily. The honest read: **either the recent regime turned against it, or the
in-sample strength was partly an artifact — ~17.5d cannot tell them apart.**

**Disposition:** the agent ships built + tested but is **NOT added to the roster**
(`configs/*.yaml`) — it is not eligible for paper→live promotion without a clean
G0 stamp, and a sweep on thin data must not be tuned to its holdout. Re-test when
a real forward-accrued 5m window exists (the only way to grow the OOS here), or
revisit once OI history (true S8) is available. The durable wins from this work
are the data-layer fixes below, which any funding-as-signal effort now needs.

## Promotion ladder
Same shape/minima as the other live agents (CI-pinned in
`tests/test_gate_minima.py`): paper→live_small with require_g0 + soak;
live_small→live on real fills. Negative-skew, so a tight loss guardrail; small
sizing; satellite to the dislocation core, never the base.
