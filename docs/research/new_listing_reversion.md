# Strategy Spec: new_listing_reversion_v1 (D2b)

> The third D2 event-driven sibling (BACKLOG D2: dislocation works because
> forced/emotional flow overshoots and reverts; build siblings). This is the
> **moonshot sleeve** of the new-listing playbook (ALPHA_ROADMAP S7): new HL
> perp listings have a recurring day-1 dynamic — forced price discovery on a thin
> book with no funding history — that overshoots fair value and then reverts.

## Thesis
A fresh perp lists with no funding history, a thin two-sided book, and a wave of
discovery flow. Price overshoots its listing reference (a pop, occasionally a
dump), then mean-reverts over the following hours/days as liquidity arrives and
perp shorts/longs arbitrage the dislocation. We fade the day-1 overshoot from the
listing reference and collect the reversion, with a **wide** hard stop bounding
the violent "it keeps mooning" tail (the kill case for any new-listing fade).

Same forced-flow → overshoot → revert structure as `dislocation_reversion_v1`
(D1), but the **reference is the listing price**, not a rolling VWAP: on day 1
there is not yet enough history for a VWAP (the vwap warmup is 30+ bars; day-1 at
1h is < 24 bars), so the standard z-score signal does not exist. The first traded
close is the only fair-value anchor available that early.

## Signal (new data-layer plumbing — the durable win of this work)
`build_frames` now emits, per coin newly listed within the window:

    view.extra["new_listings"][coin] = {age_bars, ref_px, vol_usd, recent_closes}

A coin is **newly listed** when its first candle is `>= new_listing_gap_bars`
(default 12) bars later than the EARLIEST coin in the dataset — the retention-cliff
anchor. HL serves ≤~5000 candles/interval, so all established coins' histories
start at the same cliff; a coin that starts materially later was listed *during*
the window. This requires an established anchor coin (BTC/ETH) in the universe to
fix the cliff, and is computed **independently of the vwap warmup** (a day-1 coin
has < warmup bars, so without this it would carry no signal at all). Cache v5.

- `ref_px` — first traded close (the listing reference).
- `age_bars` — bars since listing (entry gate: `age_bars <= max_age_bars`,
  default 24 ≈ day 1 at 1h).
- `vol_usd` — rolling-24h notional since listing (gate: `>= min_listing_vol_usd`,
  skips illiquid micro-listings).
- `recent_closes` — last ≤48 closes since listing (reserved for richer signals).

Entry (fade a day-1 pop; symmetric for a dump): `runup = mid/ref_px − 1`;
`|runup| >= min_runup` (default 0.25) AND `age_bars <= max_age_bars` →
**SHORT** the pop / **LONG** the dump (taker). Exit: revert to `|runup| <=
exit_runup` (default 0.08, take profit), hard stop `stop_pct` (default 0.08 —
wide, day-1 moves are violent), or `max_hold_bars` (default 24). Hard-capped
sleeve: $15/trade, $30 total, 2 concurrent — a moonshot satellite, never a core.

## Empirical findings (real HL history, 2026-06-14)
**1h candles, ~208d window, BTC/ETH anchor + the 9 perps listed in that window**
(CHIP 54d, AZTEC 123d, SKR 144d, LIT 174d, FOGO 186d, AXS/DASH ~189d, STABLE/XMR
~192d). The signal correctly flagged all 9; the agent runs end-to-end through the
production engine + scorer.

- The day-1 reversion **points the right way in-sample**: fading the overshoot
  was net-positive and the gross edge clears costs comfortably — full-sample
  +188 bps net at default params, **robust through 3× taker slippage**
  (+190.9 → +184.4 bps maker→taker-3×).
- But the **sample is far too thin to confirm anything**: only ~9 HL listings
  exist in the retrievable 1h window, yielding **6 trades total** (2 in-sample,
  4 out-of-sample) vs the G0 floor of ≥30 IS / ≥10 OOS. The in-sample-best
  walk-forward leg is +765 bps on **2 trades** (noise); the OOS leg is −78 bps
  on 4 trades. At n=6 none of this is evidence of an edge — only that the
  machinery and the thesis direction are sound.

**4h candles, ~832d window, 34 of the 91 listings in that span + BTC anchor**
(a coarser probe run only to grow the sample — there are far more listings over
830d). Here the picture **flips and turns net-negative**: fading the day-1
overshoot and holding up to a week (max_age 6 bars = 24h, max_hold 42 bars = 7d)
loses **−229 to −634 bps** (win 0–23%, 5–13 trades), is **NOT robust** to 2× slip,
and the walk-forward is incoherent (IS −741 bps / 5 trades vs OOS +814 bps /
2 trades). The longer hold catches the "it keeps mooning for a week" momentum
tail — the exact negative-skew kill case — which the intraday (≤1-day) hold at 1h
avoids. So the **sign of the edge is horizon-dependent**, and both reads are too
thin to trust: this is a caution against the naive "just fade new listings"
intuition, not a confirmation of it.

## Why this can't be confirmed from the sandbox (and what would)
Unlike D2a (where 5m's ~17.5d retention masked listings entirely), here the
signal works — the limit is **how many qualifying day-1 episodes exist in any
retrievable window**. HL's candle retention caps the lookback per interval (1h ≈
190d → ~9 listings → 6 trades; 4h ≈ 830d → 91 listings but only ~7–13 that pop
hard enough on day 1 to qualify). Finer intervals don't help (5m's ~17.5d sees ~0
listings); coarser intervals see more listings but at a day-1 granularity too
coarse to capture the intraday overshoot-and-revert (the 4h read is net-negative,
above). New listings are simply a **low-frequency, fat-tailed event** — a handful
of qualifying pops per quarter — so a confirmable OOS sample (≥10 day-1 episodes
in a held-out tail) needs history **accrued forward**, not back-fetched, AND a
fixed entry/hold horizon settled in advance so the sign isn't chosen post hoc.
The honest path to a verdict is a forward listing log on the host: record each
new perp's first-seen timestamp and 5m/1h candles from listing, and retest once
~30+ episodes accrue.

## G0 VERDICT (2026-06-14): NOT CONFIRMED — do not promote
Primarily for lack of **evidence**: the retrievable window holds too few day-1
episodes to clear the min-trade floor (6 trades at 1h vs 30/10), and what little
signal there is **changes sign with the hold horizon** (intraday fade positive on
6 trades; week-long fade net-negative on 13) — i.e. not a stable edge, a thin and
horizon-sensitive one. The agent ships **built + tested** but is **NOT added to
the roster** (`configs/*.yaml`) and is **not wired live** (see below) — it is not
eligible for paper→live promotion without a clean G0 stamp, and a study on ~6
trades whose sign flips with one parameter must never be tuned to its holdout.

**Durable win shipped regardless of the verdict:** the new-listing detector in
the data layer (`new_listings` on `Frame`/`MarketView.extra`, cache v5) — a
reusable, forward-accruable signal that any future moonshot-sleeve work (S7)
needs, and which this agent will consume unchanged once the live wiring and a
real forward sample exist.

## LIVE wiring (deferred — honest gap)
`new_listings` is computed in backtest frame assembly from full candle history.
The live `build_view` does **not** populate it yet, so the agent HOLDS in live.
Wiring it (track each perp's first-seen timestamp forward from the `meta`
universe each cycle, or a dedicated listing log) is only worth doing if/when a
forward-accrued sample confirms the edge — so it is gated on the G0 verdict, not
done speculatively. Until then this agent is inert live by construction.

## Promotion ladder (if a forward sample ever confirms)
Same shape/minima as the other live agents (CI-pinned in
`tests/test_gate_minima.py`): paper→live_small with require_g0 + soak;
live_small→live on real fills. Negative-skew and event-sparse, so: tiny moonshot
sizing, a wide stop but a hard per-trade loss cap, satellite to the dislocation
core — never the base.
