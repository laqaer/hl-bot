# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-leverage
*unblocked* thing. Add new findings as you discover them.

## P0 — find an edge (the whole point)

- [x] **B-cadence-data — Can fine-cadence (5m/15m/1m) durability research be run on HL candles? NO —
  structurally blocked by HL data RETENTION, not by tooling (Iteration 39).** The "what's next" from
  Iter 37/38 named direction (a) **sub-bar / cadence-mismatch** (REVIEW C7) as the highest-leverage
  unblocked move. Before building a fine-cadence thesis, probed the data ceiling. Two findings: **(1)
  `candleSnapshot` caps at ~5000 bars/request** (1m→3.6d, 5m→17.5d, 15m→52d, 1h→208d) AND is **anchored to
  `endTime`** (returns the most-recent block up to end; `startTime` is only a floor). **(2) HL retains only
  ~one cap of history total** — requests ending older than the trailing block return EMPTY (5m@-20d EMPTY,
  15m@-60d EMPTY, 1h-old-window EMPTY; earliest 1h = ~208d ago). So there is **no older data to page to**:
  the durability bar's 2× disjoint ~120d windows are **impossible at any sub-1h cadence** (5m gives 17.5d
  total, 15m 52d total — not even one 120d window, let alone two), and this confirms Iter-35's "1h baseline
  can't extend past ~240d" as a hard retention ceiling, not a fetch bug. **Conclusion: the (a) cadence
  direction via HL candles is dead** — fine-cadence backtesting would need an external tick/candle archive
  (forward-recording or 3rd-party), an infrastructure project, not a candle fetch. Confirms B-exec-tickmark
  is also retention-blocked (no historical fine candles either). Shipped the correct *backward* candle
  paginator anyway (`_fetch_candle_page` + `_paginate_candles` page backward from `endTime`, dedupe by open
  time, terminate when HL yields no older rows — the right implementation for HL's endTime-anchored API,
  even though retention currently bounds it to one block; +3 tests). This re-weights the remaining search
  to **(b) Path C honest measurement** as the live next move. Maker-only/research-only, no live change.
  Numbers in PROGRESS Iter 39.
- [x] **B-basis — Perp-vs-spot basis reversion (the TENTH structurally-different thesis; the LAST named
  candidate class, flagged unrun in Iter 33/37). PRUNED as a deployable edge (Iteration 38): the pooled
  sign-stable-positive point is a KNIFE-EDGE in every param AND an averaging artifact of coins whose
  per-coin sign FLIPS across windows.** HL lists spot for the wrapped majors (UBTC `@142`, UETH `@151`,
  USOL `@156`) + native HYPE (`@107`) — the only liquid perp/spot overlaps (verified vs spotMeta) — so a
  same-venue basis `b=perp/spot−1` is directly measurable. New pure `backtest/basis_reversion.py`
  (`BasisBar`/`bars_from_candles`/`simulate_basis_reversion`/`simulate_universe_basis` + `SPOT_MARKETS`,
  no-lookahead rolling-z reversion: SHORT perp when basis rich z≥+entry / LONG when cheap z≤−entry, exit on
  |z|≤exit; perp-only directional, decomposes each round-trip into perp-move − 2×maker-fee; +8 tests).
  Orthogonal to all nine pruned theses: keys off the *cross-market price gap of the same asset*, not own
  return / funding level / pairwise ratio / clock / execution. **Result (real HL, BTC/ETH/SOL/HYPE, 1h,
  maker_fee=1bp, two disjoint 120d windows):** at lb=48/entry_z=2.0/exit_z=0.5 BOTH windows are net-POSITIVE
  & sign-stable (**+1.6 / +11.1 bps/round-trip**) — the first sign-stable-positive candidate since pairs —
  BUT it is a **knife-edge**: entry_z 1.5→−1.9/2.5→−4.9 (trailing sign-flips), lookback 24/36/72 flip,
  exit_z 0.25→−5.0/1.0→trailing+ but older≈0. And the per-coin decomposition shows the pooled positivity is
  an **averaging artifact**: BTC flips −7.0(trail)→+17.6(old), HYPE flips +5.2→−7.6; the older window's pool
  is ETH/BTC-specific (+32/+18) and does not repeat. So no constituent carries a durable basis edge, and the
  trailing-window majors basis is ~zero-to-marginal — **M5's prior (majors basis tiny & well-arbitraged) is
  confirmed.** Same failure signature as pairs slice 4 (portfolio-averaging point that breaks under
  decomposition). The universe CANNOT be widened (no other liquid perp/spot overlaps on HL), so there is no
  rescue basket. Tenth thesis pruned; `basis_reversion.py` stays as a measurement tool. Maker-only, no live
  change. Numbers in PROGRESS Iter 38.
- [x] **B-exec-roundtrip — (NINTH thesis, slice 2) round-trip / inventory-skew maker quoting. DONE
  (Iteration 37): the `n_both` rescue FAILS — net-NEGATIVE & sign-stable at every half-spread, both windows,
  even WORSE per-event than the symmetric quote → the ninth (execution) thesis is PRUNED.** Added
  `simulate_maker_inventory`/`simulate_universe_inventory` + `MakerInventoryResult` to `maker_spread.py`
  (no-lookahead, ≤1 lot, skews fully against inventory: quotes both sides when flat, then only the exit side
  once a lot is held; realizes PnL per completed round-trip; unclosed lots reported & unbooked; +6 tests).
  **Result (real HL majors, 1h, maker_fee=1bp, 120d×2):** INV net **−4.9 to −6.5 bps/round-trip**, sign-stable
  across both windows & all half-spreads (2/5/10/20bps). The in-bar round-trips are genuinely adverse-free
  (`gross=2×hs`, adverse 0) and dominate by count (~70–87%), but **you can't pre-select two-sided bars** — the
  ~13–37% *carried* round-trips (single-sided fills unwound to stay ≤1 lot) average ~**64bps adverse each** and
  sink the pool. Skewing against inventory doesn't avoid adverse selection, it **defers** it
  (fill-when-wrong → unwind-when-still-wrong). Both passive spread-capture forms (symmetric slice 1 +
  inventory-skew slice 2) are net-negative & sign-stable. **The one positive structure (adverse-free in-bar
  round-trips) is real but unharvestable.** Maker-only, no live change. Numbers in PROGRESS Iter 37.
- [ ] **B-exec-tickmark — (parked, low priority; a model refinement, NOT a new thesis) sub-bar / tick-level
  fill marking.** Both maker slices mark fills to the **bar close**, a pessimistic adverse proxy (a real maker
  often recaptures the spread within seconds, so close-marking over-attributes intrabar continuation as
  adverse). The inventory model (Iter 37) partly answers this — its carried round-trips wait whole bars
  (avg hold up to 0.9 at 20bps) so much of their adverse is genuine multi-bar drift — and the structural prune
  is robust to it (even the adverse-free in-bar round-trips can't be harvested). Only worth running if a future
  iteration fetches trade-tick / L2 data anyway (overlaps the sub-bar execution angle). Maker-only, no live.
- [x] **B-exec — Execution / maker-rebate capture (the NINTH structurally-different thesis; an *execution*
  edge, not a *direction* edge). Slice 1 DONE (Iteration 36): model built + naive symmetric quote is
  net-NEGATIVE & sign-stable.** Eight direction/relative theses are pruned or reduced to an
  over-conditioned point, all sharing one failure: a price/funding/clock-derived *directional* signal that is
  regime-sensitive under walk-forward. REVIEW C1 says the structural money is in **execution, not direction**
  (the taker tax is ~73% of the bleed; B1 measured maker alone doesn't *create* direction edge, but it never
  tested capturing the *spread/rebate itself* as the edge). Thesis: a passive maker that quotes both sides
  earns the realized half-spread + any maker rebate net of adverse-selection (fill-when-wrong) cost — **not a
  directional bet**, so it may sidestep the regime-sensitivity that killed the eight directional theses.
  **Slice 1 result (real HL majors, maker_fee=1bp, no rebate):** new pure `backtest/maker_spread.py`
  (no-lookahead intrabar fill sim over real OHLC, decomposes each fill into captured-spread − adverse-drift −
  fee + rebate; 10 unit tests). The naive **symmetric** two-sided quote is **net-negative at every
  half-spread (2/5/10/20bps), BOTH disjoint 120d windows, SIGN-STABLE** (−2.6 to −3.3bps/fill; adverse runs
  ~1.5–2bps above gross) AND at 5m cadence (−3.4bps). Cleanest sign-stable result of any thesis (no direction
  = no regime to flip) but sign-stably *negative*. Not a full prune yet — the round-trip/inventory-skew
  variant (B-exec-roundtrip) is unrun and is the only positive structure the model found. Maker-only, no live
  change. Numbers in PROGRESS.md Iteration 36.
- [ ] **B-session-tod — (low priority) one untested session-timing angle: time-of-day-resolved entry.** The
  pruned B-session traded a single contiguous US-session block. The unrun variant is whether a *finer*
  time-of-day decomposition (e.g. only the US cash open hour, or excluding the lunch lull) sharpens the
  walk-forward — but given the within-window regime-sensitivity persists at a 2× baseline (slice 2) and the
  hour-band edge is a smooth hill (slice 4, no single hour is special), this is **unlikely to fix durability**
  and is parked below B-exec. Only worth running if a future iteration is out of fresher theses. Maker-only.
- [x] **B-session — Session-timing (the EIGHTH structurally-different thesis; first that keys off NEITHER
  price NOR funding). PRUNED as a deployable edge (Iteration 35): the strongest-characterized lead in the
  search, but NOT DURABLE — the within-window walk-forward regime-sensitivity is NOT a boundary/horizon
  artifact (persists at a 2× longer ~480d baseline) and it SIGN-FLIPS on a disjoint liquid-alt basket.**
  Slices 2–4 DONE (Iteration 35), all maker, real HL history: **(3) breadth — wider majors (12 coins,
  120d×2):** ❌ NOT DURABLE but **sign-stable** (+11.4/+0.6, NOTE) — widening majors does NOT break
  sign-stability (unlike the momentum lead), equity-beta hypothesis survives breadth on majors.
  **(3) breadth — liquid alts (alts_heldout, 120d×2):** ❌ NOT DURABLE and **SIGN-FLIPS** — trailing +24.4
  even *confirms* (in +22.1/oos +29.6) but older window −1.7 (artifact signature); the effect does NOT
  generalize to alts, trailing alt strength is window-specific. **(2) longer baseline — `--windows 3` 1h:**
  oldest 240–360d window is data-limited (no trades; HL 1h candle history ~208d cap), so the 1h baseline
  can't extend past ~240d. **(2) longer baseline — 4h, 240d×2 (~480d / ~1.3yr):** ❌ NOT DURABLE,
  **sign-stable** (+3.2/+2.7, NOTE) but OOS tail negative in BOTH windows (trailing in +10.7/oos −13.8;
  older in +4.1/oos −0.6) — **definitive: the within-window regime-sensitivity is NOT a boundary artifact,
  it persists at 2× the baseline.** **(4) hour-band sweep (enter 12–16Z, exit 21Z fixed, 120d×2):** NO
  PLATEAU by the binary durability criterion (no value clears full durability — expected for a
  regime-sensitive lead), BUT the full-sample edge is a **smooth, contiguous-positive hill cleanly peaked
  at the a-priori 14Z open** (12→+4.4, 13→+9.5, 14→+11.4, 15→+8.4, 16→+5.0) — **not a single-hour knife-edge,
  not data-mined to one lucky hour.** **Net:** session-timing is the strongest lead yet — sign-stable on
  majors across windows AND a 2× baseline, mirror-coherent (slice 1), hour-robust, breadth-robust on majors —
  but it has the same fatal regime-sensitivity as the majors-1d momentum lead (now confirmed NOT a boundary
  artifact) PLUS an alt-basket sign-flip. Eighth thesis fully characterized and pruned; `session_timing_v1`
  stays in the roster for paper/measurement only. **One untested low-priority angle remains** (see B-session-tod
  below). Maker-only, no live change. Numbers in PROGRESS.md Iteration 35. History (slice 1):
  only inside an a-priori-fixed UTC hour band (default US equity session 14–21Z, weekdays), flat outside;
  pure `in_session(ts_ms,...)` reads only the bar's UTC hour+weekday (zero price/funding), `invert` flag
  trades the complement. Registered in confirm/backtest, 8 unit tests. **Result (majors, 1h, 120d×2 windows,
  maker):** ❌ NOT DURABLE but a genuine LEAD — base long-US-session edge is **positive & SIGN-STABLE** both
  windows (trailing +11.4 in+17.0/oos−1.3; older +0.4 in−3.2/oos+8.9; harness "lead, not artifact" NOTE), and
  the **mirror is clean** — invert (long overnight/weekend) is **negative in BOTH windows** (−6.3 / −26.6, no
  sign-flip). So across two disjoint 120d windows majors drift up in the US session / down overnight — a
  coherent repeatable clock effect, stronger cross-window coherence than any pruned thesis. **Caveat (why a
  lead, not a deploy):** base case still fails the within-window walk-forward (trailing edge lives in-sample
  then evaporates OOS; older window inverts) — same regime-sensitive failure *mode* as the majors-1d momentum
  lead, NOT the artifact sign-flip. Joins the "sign-stable lead, not deployable" bucket. **Push-slices to
  run:** (2) `--windows 3` + longer per-window `--days` (boundary-artifact check); (3) basket breadth (wider
  majors + liquid alts — is it equity-beta-specific or general?); (4) `--sweep` enter/exit hours around the
  a-priori band (confirm a contiguous hour-plateau, not a knife-edge). Maker-only, no live change. Numbers in
  PROGRESS.md Iteration 34.
- [x] **B-pairs — Pairs / relative-value mean-reversion (the seventh, structurally-different
  thesis): THE FIRST SIGNAL TO CLEAR THE CANONICAL DURABILITY BAR.** Slice 1 DONE (Iteration 29).
  New `pairs_reversion_v1`: market-neutral statistical arbitrage on the **log-ratio spread** of a coin
  pair (ETH/BTC, SOL/AVAX, LINK/AAVE) vs its rolling-z mean — SHORT the rich leg / LONG the cheap leg,
  hold until the spread reverts inside the band. Orthogonal to all six pruned theses: keys off a
  *pairwise relationship*, not a coin's own return (momentum) or funding level (carry). Registered in
  confirm/backtest, 7 unit tests. **Result on real HL history (6 coins, 1h, lb=48, maker):**
  (a) **`--windows 2` 120d = DURABLE** — trailing full +5.3bps (in +6.1 / oos +3.4), older full +8.0
  (in +7.1 / oos +9.7); *both windows in+oos positive* — **no prior thesis ever passed this**.
  (b) Across EVERY config tried the full-sample maker edge is **sign-stable & positive** (never the
  artifact sign-flip): windows=2/180d (+5.6 / +14.0, NOTE fires), 2-pair-only (+9.5 / +7.8).
  **Caveats (why a lead, not a deploy):** maker-only (taker-1x ≈ −0.2bps breakeven, taker-2x −2.2);
  `--windows 3` fails on a no-trade oldest 120d slice; at 180d / narrower baskets the trailing OOS tail
  weakens to the "regime-sensitive, sign-stable lead" failure mode (the harness NOTE, not the artifact).
  **A genuine lead to push, not a prune; maker-only, nothing touches capital.** Numbers in PROGRESS.md
  Iteration 29. **Slice 2 DONE (Iteration 30): plateau-sweep harness + run on the lead.** Built reusable
  `sweep_param`/`classify_plateau` (+ `confirm --sweep`, 13 tests): a PASS is robust only if a *contiguous
  run* of ≥2 adjacent param values passes, else knife-edge. Run on the lead (120d×2 windows, maker, DURABLE
  = passing): **lookback_bars is a narrow PLATEAU** (lb∈{48,52,56} all durable +5.3/+5.6/+4.0; <48 weakens,
  ≥60 trade-starves) — good, not a single-point fit; but **entry_z is a KNIFE-EDGE** (only 2.0 durable,
  though the full maker edge is smooth/sign-stable and cleanly peaked at 2.0: 1.5→+2.5 … 2.0→+5.3 …
  2.5→+1.6). Net: the canonical-bar PASS needs lb∈[48,56] AND entry_z≈2.0 — a lead, but less robust than
  one number suggested. Numbers in PROGRESS.md Iteration 30. **Slice 3 DONE (Iteration 31): held-out pair
  set — THE LEAD DOES NOT GENERALIZE.** Pinned the held-out universe as a named pair-basket (`PAIR_BASKETS`
  + `resolve_pairs`/`coins_in_pairs` in baskets.py, wired into confirm/backtest, +8 tests) so the result
  cites an auditable universe. Ran the canonical 120d×2-window maker durability bar (lb=48, entry_z=2.0,
  the Iter-29 config) on **disjoint liquid pairs** (`pairs_heldout` = ARB/OP, APT/SUI, DOGE/WIF — two L2
  govs, two Move L1s, two memes; no leg overlaps the default ETH/BTC|SOL/AVAX|LINK/AAVE): **❌ NOT DURABLE
  and it FLIPS SIGN** (trailing full −4.7bps / older +5.8 — the artifact signature the bar was built to
  catch). Dropping the weak meme leg and running the two *strongly-cointegrated* held-out pairs alone
  (ARB/OP|APT/SUI) is **worse**: trailing full −6.9bps (in −6.6 / oos −6.8, cleanly negative), older +0.4,
  still sign-flips. (Default basket re-reproduced this run: +5.3 / +8.2 DURABLE — the wiring is sound, the
  lead is real *on its basket*.) **Conclusion: the Iter-29 edge is basket-specific, not a property of
  pairs-reversion as a class.** It does not survive leave-pairs-out — the demonstrated edge lives in the
  three default pairs (most plausibly ETH/BTC's strong cointegration), and a disjoint liquid set produces
  the same cross-window sign-flip that hand-pruned the six earlier theses. This is a major fragilization:
  the only candidate to ever clear the bar clears it **only on the basket it was specified with**. Not yet
  a full prune (the default basket genuinely passes the bar and the plateau), but the strategy-class
  generalization claim is **dead**. Numbers in PROGRESS.md Iteration 31. **Slice 4 DONE (Iteration 32):
  leave-one-pair-out within the default basket — DURABILITY IS A 3-PAIR COMBINATION EFFECT, NOT a one-pair
  bet, but it BREAKS UNDER ANY LEAVE-ONE-OUT.** Added pure `leave_one_pair_out` + a one-command `confirm
  --leave-one-out` flag (loads history once, runs the bar on the full basket / each single pair / each
  leave-one-out subset), +3 tests. Result (exact Iter-29 config, maker, 120d×2): the full 3-pair basket is
  the **only** durable variant (+5.3/+8.2); **no single pair and no leave-one-out triple is durable**.
  Crucially it is **not "just ETH/BTC"** (ETH/BTC alone is the *weakest* relevant single +3.0; SOL/AVAX
  alone strongest +15.3/+6.9) — that hypothesis is disproved — but the PASS survives **only** when all
  three spreads are pooled (LINK/AAVE alone and the ETH/BTC|LINK/AAVE pair-out even *sign-flip*). That is a
  **portfolio/averaging effect**: pooling 3 imperfectly-correlated spreads smooths the walk-forward enough
  to pass even though no constituent does. **Net: the canonical-bar PASS is a single-basket knife-edge** —
  conditioned on lb∈[48,56] AND entry_z≈2.0 AND exactly these 3 pairs (all required), and it fails on
  disjoint liquid pairs (slice 3). A heavily over-conditioned point, **not a deployable book**. Numbers in
  PROGRESS.md Iteration 32. **Slice 7 DONE (Iteration 33): the "larger pre-committed diversified book"
  reframe — THE LAST RESCUE ANGLE FAILS.** Added `pairs_diversified` (= the exact union of pairs_default ∪
  pairs_heldout, 6 pairs / 12 distinct legs / six economic buckets; ZERO new pair choices, the
  maximally-defensible inverse of leave-one-out) + 2 tests, and ran the canonical 120d×2-window maker bar on
  it. **❌ NOT DURABLE and it SIGN-FLIPS** (trailing full −0.1bps in −0.7/oos −1.4; older +3.4 in −0.1/oos
  +11.4). Pooling *more* imperfectly-correlated spreads does **not** increase durability — the held-out
  half's negativity drags the pool to ~zero on the trailing window and reintroduces the artifact sign-flip.
  This **disproves the portfolio/averaging rescue**: slice 4's smoothing was specific to the 3 default
  pairs, not a general "more pairs = more durable" property. **Conclusion: pairs-reversion is now fully
  pruned as a deployable edge.** The only PASS (the 3-pair +5.3) is a heavily over-conditioned single point
  (lb∈[48,56] AND entry_z≈2.0 AND exactly those 3 pairs); it fails leave-pairs-out (3), leave-one-pair-out
  (4), AND pre-committed diversification (7). Remaining slices (5)/(6) are moot — they could only widen the
  param plateau, never fix basket-specificity. Numbers in PROGRESS.md Iteration 33. **Pairs stays maker-only
  paper; the next move is a fresh structurally-different thesis.** Pairs investigation CLOSED.
- [x] **B1 — Quantify the taker tax on every agent.** DONE (Iteration 16, network
  open). 120d/1h over BTC,ETH,SOL,HYPE,AVAX,LINK: taker tax ≈ **5.7 bps round-trip**,
  ~**73% of the TWAP bleed** (twap_mr −7.7→−2.0 bps taker→maker; regime −8.0→−2.3).
  But maker alone doesn't create edge: `confirm --prefer maker` → **NOT CONFIRMED**
  (flat in-sample, negative OOS). Carry/femr are dormant on majors (realized funding
  peaks ~57% APR < their 88–130% APR thresholds) and net-negative even when forced
  to trade. **No agent passes G0 on majors.** Numbers in PROGRESS.md Iteration 16.
- [x] **B1-alt — Test the carry thesis on HIGH-FUNDING alts.** DONE (Iteration 17).
  First fixed a real measurement bug: `fetch_funding_history` was 500-row capped
  (~21d), so any longer carry backtest silently read funding=0 on older frames.
  Paginated it → full 120d funding (now unit-tested). Then fetched a high-funding
  alt basket (INJ/PURR/TRUMP/AERO/NIL/APT/SPX/PYTH/EIGEN/S, realized 14–48% APR
  |funding|) and ran `confirm --prefer maker` with the volume gate lowered so the
  alts aren't filtered out. **Result: NOT CONFIRMED, both agents.** xfund_carry oos
  edge −16.8bps (sharpe −2.95); funding_carry oos −33.2bps. A selectivity sweep
  (raise enter threshold 26→175% APR) is net-negative at **every** level and per-trade
  edge_bps gets *worse* as you pick the most-extreme funding — the carry collected is
  smaller than maker cost + the directional noise of the (imperfectly neutral) legs.
  **The carry thesis is pruned: no G0 on majors (B1) OR high-funding alts.** Numbers
  in PROGRESS.md Iteration 17.
- [x] **B-mom — Cross-sectional momentum (the structurally-different signal).** DONE
  (Iteration 18). New `xsect_momentum_v1`: market-neutral, ranks the universe by
  trailing return over a lookback, LONG top-K / SHORT bottom-K, maker-friendly; a
  `reversion` flag flips both legs to test short-horizon mean-reversion in the same
  book. Registered in `confirm`/`backtest`; 5 unit tests. **Result: NOT CONFIRMED on
  majors OR high-funding alts, neither momentum nor reversion.** The cross-sectional
  edge *flips sign* between the in-sample (older 70%) and OOS (recent 30%) windows —
  MOMENTUM in −4.7→oos +15.8bps; REVERSION in +2.7→oos −17.8bps (majors, maker);
  same mirror on alts. A regime inversion mid-window, which walk-forward correctly
  rejects. Maker full-sample ≈ flat (+0.8/−0.7bps); taker-2x firmly negative. Numbers
  in PROGRESS.md Iteration 18.
- [x] **B-mom-regime — Regime-gate cross-sectional momentum. PRUNED by B-mom-regime-validate
  (Iteration 20): the G0 PASS was window-specific — it reverses sign on a fresh out-of-time
  window. Kept the (default-off) gate code; no live use.**
  DONE-slice (Iteration 19). Added a causal, default-off `regime_gate` to
  `xsect_momentum_v1`: only run the dollar-neutral book when the equal-weighted
  universe trailing return over `regime_lookback` bars ≥ `regime_min_return` (a-priori
  momentum-crash avoidance — momentum reverses after market bottoms). On **high-funding
  alts** this **flips momentum from un-tradeable to a G0 PASS** in base config
  (regime_lookback=12, thr=0): in-sample +4.4bps/sh+1.63, **oos +16.0bps/sh+3.38**,
  full-sample maker **+8.4bps** over 1742 trades, taker-2x ≈ break-even (+0.9). The
  ungated agent sign-flipped (in −2.6 / oos +10.0) and failed — the gate is what makes
  it tradeable forward. Robustness: **PASS at every walk-forward split** (oos_frac
  0.2–0.5, both windows +, oos sharpe +2→+4.3) and **leave-one-coin-out** keeps maker
  edge +5.6→+10.4bps in all 10 folds (no single-coin dependence). Caveats keeping this
  a *candidate not a deploy*: (a) **alts-only** — majors momentum in-sample stays
  negative at every setting; (b) **maker-only** — taker-2x hovers ±2bps; (c) **marginal
  at the gate** — in-sample ~+3bps, so the binary PASS toggles under leave-one-out;
  (d) **regime_lookback-sensitive** — only ~12–18 pass, 24+ fail in-sample. Numbers in
  PROGRESS.md Iteration 19.
- [x] **B-mom-regime-validate — Harden the alts-momentum lead → PRUNED (Iteration 20).**
  Added out-of-time window support to the fetch (`window_bounds`/`end_ms` plumbing +
  `backtest-fetch --end-offset-days`, unit-tested) so a *disjoint* window can be pulled.
  (1) **out-of-time FAIL:** refetched the immediately-preceding 120d (ends 2026-02-09,
  same alts basket) and re-confirmed the regime-gated agent — it **reverses sign**:
  in −7.4 / oos −9.4 / maker full **−7.8bps** (vs +8.4 on the trailing window). A real
  edge survives a fresh window; this does not. (2) **held-out basket** (disjoint liquid
  alts SUI/SEI/TIA/WLD/ARB/OP/ENA/JUP/LDO/AAVE, recent window): **marginal, NOT
  CONFIRMED** — in +2.3bps (below the +3 gate), oos +7.3, maker full +4.2bps, negative
  at every taker level. The Iteration-19 +8.4bps was **window-specific**, not durable;
  the regime gate fixes the in/oos sign-flip *within* the recent window but can't make
  the agent survive a genuinely different time period. (3) plateau map is **moot** — no
  point mapping a plateau of a window-specific artifact. **The momentum lead is pruned.**
  Numbers in PROGRESS.md Iteration 20. No live change.
- [x] **B-mw — Multi-window robustness harness (the out-of-time bar as code).** DONE
  (Iteration 21). Iteration 20 proved trailing-window G0 is *necessary but not sufficient*
  (regime-momentum +8.4→−7.8bps maker on the prior 120d). `confirm_across_windows`
  (in `backtest/confirm.py`) runs `confirm_strategy` on N disjoint windows and returns a
  single **DURABLE/NOT-DURABLE** verdict: durable iff ≥2 windows, *every* window confirmed,
  and the preferred-execution full-sample edge never flips sign (a sign flip is the artifact
  signature, called out explicitly). `MultiWindowResult` + `preferred_full_sample` helper;
  3 unit tests (survives-every-window → durable; sign-flip → not durable + flagged;
  single-window → never durable, the trailing-only trap). This is now the standard bar every
  future candidate must clear. Numbers in PROGRESS.md Iteration 21.
- [x] **B-mw-cli — Wire the durability bar into `hlbot confirm` (one command).** DONE
  (Iteration 22). `confirm --windows N` (N>=2) now runs `confirm_across_windows` over N
  disjoint, back-to-back `days`-long windows (trailing + N-1 older, fetched via the
  `end_ms` plumbing) and prints a single DURABLE / NOT DURABLE verdict; `--windows 1`
  (default) keeps the legacy single-window PASS/FAIL. Extracted a pure `_window_specs`
  helper (newest-first, disjoint, back-to-back) and unit-tested it (+2). The out-of-time
  bar that hand-pruned the Iteration-19/20 momentum lead is now a single adversarial
  command — every future candidate runs `confirm --windows 2+` before any paper/live talk.
  Numbers in PROGRESS.md Iteration 22.
- [x] **B-tsmom — Time-series (absolute) momentum: the structurally-different
  directional signal. PRUNED (Iteration 23).** New `ts_momentum_v1`: trades each coin
  independently on the sign of its *own* trailing return (LONG up-trend / SHORT
  down-trend), so the book takes *net directional* exposure — the orthogonal class to
  the four pruned dollar-neutral cross-sectional ranks; canonical trend-following (CTA)
  edge. Registered in `backtest`/`confirm`, 6 unit tests. Run through the durability bar
  (`confirm --windows 2 --prefer maker`) from the *first* run, not a single trailing
  window. **Result: NOT DURABLE on majors AND high-funding alts.** Same artifact
  signature as B-mom: trailing window marginally positive (majors full +2.8 / alts +2.4)
  but **in-sample negative** (−2.7 / −5.4 → oos +15.4 / +21.2 — a mid-window regime
  inversion), and the older 120d window **flips sign** (majors −4.6 / alts −10.6). Five
  structurally-different theses now pruned after the out-of-time bar. Numbers in
  PROGRESS.md Iteration 23.
- [x] **B-horizon — Push the majors 1d cross-sectional-momentum lead over G0. PRUNED (Iteration 27):
  the last two slices both fail to advance it.** Slice (4) **longer per-window `--days`** (360d/windows=2,
  4 majors): still **NOT DURABLE** — full-sample is **sign-stable** (+20.2 / +26.7bps, the harness NOTE
  fires) but each window still fails its own walk-forward (trailing oos +5.0 weak; older in −20.3 → oos
  +104.6 — a regime inversion just *relocates* to a different half). Lengthening the window does NOT
  shrink the OOS-tail problem, disproving the boundary-artifact hypothesis. Slice (5) **widen the majors
  basket** (12 coins: +DOGE/XRP/LTC/BNB/AVAX/LINK/SUI/AAVE, 240d/windows=3): **actively worse** — full
  +21.8 / −3.4 / +133.3bps now **FLIPS SIGN** (the artifact signature), breaking the one good property
  (sign-stability) the 4-coin basket had. Breadth doesn't help; it hurts. **Conclusion:** majors-1d
  cross-sectional momentum (lb=14) is a real, cost-surviving, sign-stable signal on the *trailing* narrow
  basket, but it is **regime-sensitive within every window** and never clears the durability bar across
  lookback-plateau, window length, OR basket breadth. Sixth structurally-different thesis characterized
  and pruned after the out-of-time bar. majors-only. No live change. Numbers in PROGRESS.md Iteration 27.
  History below.
  LEAD PERSISTS,
  still NOT DURABLE (Iteration 26). Slice (1) **1d lookback sweep** DONE (Iteration 25): a **12–15-bar
  plateau** CONFIRMS the **trailing** 240d window (lb=14: in +49.1/oos +42.7bps) and is
  **taker-survivable** (maker +46.2 → taker-3x +36.7bps). Slice (2) **regime-gate at 1d** DONE
  (Iteration 26): the Iteration-19 causal `regime_gate` (a market-drawdown filter) **does NOT rescue
  the older window** at any `regime_lookback` (12/24/36/48) — its OOS tail still reverses (rl24 older
  oos −16.3 even as in jumps to +211.7), and rl12 *breaks* the trailing window (sign flip). The older
  window's failure is a **momentum crash on a market rebound**, which a stand-aside-in-drawdown gate
  structurally can't catch. Slice (3) **`--windows 3`** DONE (Iteration 26): full-sample edge is
  **positive in ALL THREE disjoint 240d windows** (+46.2 / +8.3 / +11.6 — ~2yr, **no sign flip**),
  but windows 2 & 3 each fail their own walk-forward (a regime inversion in one half). The harness now
  **distinguishes this failure mode** (sign-stable lead) from the artifact sign-flip via a new
  `sign_stable` diagnostic + explicit NOTE. Remaining slices: (4) **longer per-window `--days`** so each
  window's OOS tail is a smaller fraction (tests if the walk-forward failure is a boundary artifact);
  (5) **widen the majors basket** to see if the plateau + no-sign-flip hold on more coins. **Alts at 1d
  stay pruned.** majors-only. Numbers in PROGRESS.md Iteration 26. No live change.
- [x] **B-femr-regime — femr retired from the live roster.** DONE (Iteration 28).
  femr's 130%-APR entry never trips on liquid coins (B1) and funding carry has no
  net-of-cost edge even on high-funding alts (B1-alt), so it's dormant and edgeless.
  Added `RETIRED_LIVE_AGENTS` (in `cli/main.py`): a documented registry that hard-blocks
  retired agents from the live execution roster **regardless of agent_state** (so even an
  accidental live_small/live promotion can't place femr orders), surfacing an auditable
  skip reason. femr stays in the roster for paper evaluation/ongoing measurement; this is
  tightening-only. +1 test (retired agent blocked even when promoted to live/enabled);
  existing live-gate test re-pointed off femr. Numbers in PROGRESS.md Iteration 28.
- [x] **B-baskets — Canonical named coin baskets for reproducible backtests.** DONE (Iteration 27).
  Every recorded confirm/backtest number is only honest if its exact universe is known, but the search
  hand-types baskets (majors, alts_highfunding, alts_heldout, majors_wide) on every run — one typo
  silently changes a result. New pure `backtest/baskets.py`: `BASKETS` presets (pinned to the iterations
  that used them) + `resolve_basket(spec)` that expands preset names and passes bare symbols through
  (backward compatible: `--coins BTC,ETH` unchanged, `--coins majors` expands, `--coins majors,DOGE`
  mixes/dedupes). Wired into `confirm`/`backtest`/`backtest-fetch` (research commands only; live tick
  paths untouched). +7 unit tests. Numbers in PROGRESS.md Iteration 27.
- [x] **B-params — `--params` config override for `confirm`/`backtest`.** DONE (Iteration 25).
  The factories hardcoded `config={}`, so every parameter sweep meant editing code. New
  `--params 'lookback_bars=14,enter_return=0.05'` threads a typed override dict (pure
  `_parse_agent_params`, int→float→bool→str inference, +6 unit tests) into the agent factory.
  Unblocked the B-horizon lookback sweep that found the 12–15-bar daily plateau. Numbers in
  PROGRESS.md Iteration 25.
- [x] **B-fetch-retry — 429-resilient history fetch.** DONE (Iteration 24). `fetch_candles`
  + `_fetch_funding_page` route their POST through `_request_with_retry` (exponential backoff
  on 429/5xx, honors `Retry-After`, pure/testable). Longer/larger windows (1d/240d ≈ 12 funding
  pages/coin) were dying on HL's rate limiter and losing the whole window; this unblocked the
  longer-horizon research (B-horizon). +4 unit tests. Numbers in PROGRESS.md Iteration 24.
- [x] **B1a — Offline history cache.** Done: `hlbot backtest-fetch` +
  save/load/cached_or_fetch under `data/backtest_cache/` (gzipped JSON,
  gitignored); `hlbot backtest --cache` runs without network. (Iteration 2.)
- [~] **B2 — Maker (post-only) execution.** Primitive done: `place_limit_order`
  (post-only 'Alo'), `round_price_to`, `has_resting_order` + tests. **Remaining
  (B2b):** async resting-order fill reconciliation across ticks, then route live
  *entries* through maker (exits stay taker). Until then live entries are still
  taker. Backtest passive strategies as maker once B1 unblocks.
- [x] **B3 — `twap_mr_regime_v1`.** Done: new agent consults
  `regime_allows_fade`; closes plumbed through Frame + live `_enrich_view`;
  backtest tests prove it beats baseline on a trend. Thresholds need real-data
  tuning under B1. (Wires REVIEW C3.)
- [x] **B2b — Maker live execution.** Done: cross-tick resting-order lifecycle
  (`exec/maker.py`: rest → fill-detect → place / cancel-stale), `cancel_order`,
  and `femr_tick --execution maker` (default taker; exits stay taker). Logic
  unit-tested offline; first live use needs watching at tiny size. Next: book-aware
  limit pricing (post at touch/microprice, not mid).
- [x] **B4 — Maker carry strategies.** Done: `funding_carry_v1` (single-name
  hold-to-collect, no TP churn) and `xfund_carry_v1` (market-neutral
  cross-sectional). Engine `liquidate_at_end` folds held funding into the realized
  scorecard. Confirmed on synthetic funding; need real-data G0 (B1). (Iteration 3.)
- [x] **B5 — Confirmation harness (G0 as code).** Done: `backtest/confirm.py`
  walk-forward + cost ladder (maker/taker 1×/2×/3×) → PASS/FAIL; `hlbot confirm`.
  (Iteration 3.)
- [x] **B4-RUN — Confirm carry strategies on real history.** DONE (Iteration 17 via
  B1-alt). Majors (Iteration 16): dormant / net-negative when forced. High-funding
  alts (Iteration 17): both agents trade plenty but are **NOT CONFIRMED** as makers
  (xfund oos −16.8bps, funding_carry oos −33.2bps), negative across the whole cost
  ladder and every funding-selectivity threshold. Carry has no demonstrable
  net-of-cost edge on either universe.

## P1 — honest measurement (so the supervisor can trust itself)

- [x] **B6/B7 — Per-agent funding attribution + Sharpe.** Done: funding split to
  the agent holding the coin at funding time (scoring includes it in net/edge);
  per-agent Sharpe from daily PnL so sharpe-gates evaluate. Tested. (Iteration 7.)
  **B6-size (Iteration 10):** the split is now **size-weighted** — each funding
  payment divides among holders in proportion to their fill-derived |net size|
  (a 3× holder collects 3× the funding), with an equal-among-decision-log-holders
  fallback when no fills exist. Closes the last imprecision in the carry scorecard.
- [ ] **B6old — (superseded by B6/B7 above)** Map `funding_payments` to the agent
  holding the position at funding time (via cloid→position replay), so
  `scoring.metrics` includes funding in each agent's net. (REVIEW C4.)
- [x] **B7 — Per-agent equity curves + Sharpe/DD.** Sharpe done (Iteration 7,
  daily-PnL). **Configs standardized (Iteration 12):** removed the dead
  `max_drawdown` demote guardrail from `funding_arb_v1.yaml` (max_drawdown/calmar
  are account-only — always N/A for a real agent, so it could never fire); demote
  now keys on the computable `edge_bps`. **B7-dd done (Iteration 14):** added
  `Scorecard.max_drawdown_usd` — the peak-to-trough *dollar* give-back of the
  cumulative net-PnL curve. The design decision the capital-base concern called
  for: a *fractional* DD needs a capital base, but a *dollar* DD does not, so it's
  computable for every real agent and gateable by the supervisor. track_record
  reuses it (single source of truth). (REVIEW C5.)
  **B7-dd-gate done (Iteration 15):** wired a tightening-only
  `max_drawdown_usd` 7d *demote* guardrail into all six real-agent configs
  (thresholds scaled to each agent's 24h pause limit). Catches a run-up-then-bleed
  give-back that the edge_bps/net_pnl gates miss; tested it fires while edge/net
  stay positive.
- [x] **B8 — Record actual fill price.** Done: `femr_tick` now logs the confirmed
  `res.avg_px`/`res.filled_sz` on fill so stops/TPs key off the real entry. (M1.)
- [x] **B9 — fills→positions replay.** Done: `db/positions.py`
  (`replay_positions` pure fn + `rebuild_positions` writer) folds the exchange
  fills stream into the `positions` table — net size, size-weighted entry,
  exchange realized PnL, fees — keyed by (agent, coin); wired into `ingest_fills`.
  Survives partial fills/flips/manual interference. Tested (7 cases). (Iteration 9.)

## P2 — cadence, structure, devops

- [x] **B10 — WebSocket market view.** Done: `ingest/ws.py` MarketState +
  `hlbot ws` service writes a snapshot; live tick overlays it (HLBOT_WS_SNAPSHOT)
  for sub-second mids, L2 book_top, and a real liquidations feed (fixes C6), with
  REST fallback. Next: userFills WS for instant maker-fill detection. (Iteration 5.)
- [x] **B-book — Book-aware maker pricing.** Done (Iteration 8): `maker_limit_price`
  joins the near touch from the WS L2 book; live maker entries price off
  `view.book_top` (fallback mid, never cross).
- [x] **B11 — Retire or feed liq_cascade.** Done: removed the dead
  `{"type":"liquidations"}` REST call (a non-existent HL endpoint, C6 root cause)
  from `_enrich_view`; liquidations now come solely from the WS feed (B10), with
  `liquidations` defaulting to `[]` so the agent safely holds when unfed. Added a
  tick warning when liq_cascade is in the roster but `HLBOT_WS_SNAPSHOT` is unset
  (effectively disabled), plus `tests/test_liq_cascade.py` proving fed→enters /
  unfed→holds / thin-coin & stale-event filters. (Iteration 11.)
- [x] **B12 — Consolidate execution paths.** Done: extracted
  `runtime.collect_decisions` — the single decision-gathering core both
  `run_tick` (paper tick) and `cli.femr_tick` (live loop) now call. Live previously
  had its own loop with **no try/except**, so one agent raising in `decide()`
  crashed the whole tick; it now logs an `error` decision and continues, same as
  the safe wrapper. `defer_actions` parametrizes the place/flatten/hold
  "log-after-fill" behavior. Tested (3 cases). (Iteration 13, REVIEW M3.)
- [x] **B13 — Move hardcoded trader address to config.** Done (Iteration 6):
  `_resolve_trader_address` (HL_TRADER_ADDRESS → HL_ADDRESS → legacy). (REVIEW M6.)
- [x] **B14 — Go-live runbook in-repo.** `docs/GO_LIVE.md`: gated checklist,
  secrets/env, promote/kill-switch/rollback, monitoring. (REVIEW D3.)
- [ ] **B14a — Deploy automation.** Codify the EC2/systemd/Hermes cron + DB sync
  (without secrets) so the live loop is reproducible from the repo.

## P3 — capital formation (Path C)

- [x] **B-allocator-packet — The complete allocator deliverable in one bundle (the Iter-40 named next
  move (a)). DONE (Iteration 41).** With the edge-search artifact (Iter 40) and the track-record export
  (B15) both shipped as separate pure reports, Path C still lacked the *one document* an allocator (or the
  go-live gate) reads end-to-end: the deployment chassis + the live record + the negative edge search,
  together. New pure `reports/allocator_packet.py` composes the two existing reports and adds a frozen,
  audited `CHASSIS` record (the REVIEW "What's good" strengths — cloid attribution, ground-truth accounting,
  order safety, supervisor semantics, risk scaling, research hygiene — each citing a real source module).
  `build_allocator_packet(conn)` → {headline, chassis, track_record, edge_search}; `to_markdown` renders all
  three sections (composing `track_record.to_markdown` + `edge_search.to_markdown` verbatim, no new numbers);
  `export` writes `allocator_packet.{json,md}`. Wired as `hlbot allocator-packet` (read-only on the DB,
  mirrors track-record/edge-search). The honest headline states capital is NOT warranted until a strategy
  clears G0–G3 — the packet is evidence for the gate, not a solicitation. +4 tests (every chassis source is a
  real file; both sub-reports carried faithfully; markdown renders all three sections + every chassis source;
  JSON/MD export round-trip). No `data/` writes committed; no strategy/roster/live-mode change. Numbers in
  PROGRESS Iter 41.
- [x] **B-edge-summary — Edge-search summary report (the negative-result finding as a publishable
  artifact). DONE (Iteration 40).** With all ten structurally-different theses pruned and fine-cadence
  research retention-blocked (Iter 39), the honest next-unblocked move named by Iter 38/39 was to turn the
  negative-edge finding into a clean Path-C deliverable. New pure `reports/edge_search.py`: a frozen,
  auditable `THESES` record (the 1→10 enumeration — TWAP-MR, funding carry, x-sect momentum, regime-gated
  momentum, ts-momentum, majors-1d momentum, pairs, session-timing, maker execution, perp/spot basis), each
  row citing its backlog id + PROGRESS iteration + universe + durability bar + recorded net-of-cost headline
  + prune reason, plus the Iter-39 retention boundary (why the search is exhausted, not paused).
  `build_edge_search`/`to_markdown`/`export` → edge_search.{json,md}; wired as `hlbot edge-search`. +7 tests
  (1→N numbering no gaps, unique backlog keys, class breakdown = 8 directional + 1 execution + 1 cross-market
  matching the Iter-38 narrative, all-pruned invariant, markdown renders every row, JSON/MD export round-trip).
  Pure data + rendering, no network/DB; an allocator or the go-live gate can now read *what was searched, on
  what universe, over what windows, and why each was rejected* in one command. No live change. Numbers in
  PROGRESS Iter 40.
- [x] **B15 — Public-grade track-record export.** Done: `reports/track_record.py`
  + `hlbot track-record` → track_record.{json,md} (equity curve, Sharpe/DD/edge,
  per-agent). Chart export still TODO. (Iteration 2.)
- [ ] **B16 — Hyperliquid vault evaluation.** Spike: requirements, fees, risk of
  running an HL vault; gate behind a real track record (G3).
- [ ] **B17 — Moonshot sleeve spec.** Design the ring-fenced, loss-bounded Path B
  sleeve (separate sub-account, hard cap, defined max loss). Spec only; no live.

## Done

- [x] **B0 — Backtest harness.** `src/hl_bot/backtest/{engine,data}.py` +
  `hlbot backtest` + tests. Replays real `decide()` with cost/funding model,
  scores via production `score_agent`, computes equity-curve Sharpe/DD, with a
  `maker` flag to quantify the taker tax. (Iteration 0.)
- [x] **B-CI — Fix red CI.** Ruff B007 in `scripts/daily_scorecard.py`; aligned
  `make lint` to include `scripts`. (Iteration 0.)
- [x] **B8 — Real fill px/sz on confirmed fills.** (Iteration 1.)
- [x] **B3 — twap_mr_regime_v1** with proving backtest tests. (Iteration 1.)
- [x] **B2 (primitive) — post-only maker order path** + tick rounding + tests.
  Async fill reconciliation tracked as B2b. (Iteration 1.)
- [x] **B14 — docs/GO_LIVE.md** go-live runbook. (Iteration 1.)
- [x] **B1a — offline history cache** + backtest-fetch CLI. (Iteration 2.)
- [x] **B15 — track-record export** + track-record CLI. (Iteration 2.)
- [x] **Pilot prep** — twap_mr_regime_v1 wired into the live roster (paper
  default), registered for attribution/reporting, with a goals config. The
  operator-only live switch is documented in docs/GO_LIVE.md. (Iteration 2.)
- [x] **Unattended docs** — ralph/README "Unattended operation". (Iteration 2.)
- [x] **B5 — confirmation harness** (`hlbot confirm`, walk-forward + cost ladder).
  (Iteration 3.)
- [x] **B4 — carry strategies** xfund_carry_v1 + funding_carry_v1 + engine
  liquidate-at-end. (Iteration 3.)
- [x] **B2b — maker live execution lifecycle** + `--execution maker`. (Iteration 4.)
- [x] **B-INFRA — docs/INFRA.md** 24/7 deploy + signal/execution investment guide.
  (Iteration 4.)
- [x] **Ops automation — `hlbot health` (heartbeat) + `hlbot doctor` (preflight).**
  (Iteration 5.)
- [x] **B14a — deployment automation** (`deploy/`: install.sh, systemd units,
  Litestream, loop service, run-tick). (Iteration 5.)
- [x] **B10 — WebSocket market view + live liquidations.** (Iteration 5.)
- [x] **B13 — HL_TRADER_ADDRESS via env** (no more hardcoded account). (Iteration 6.)
- [x] **hlbot-ws.service** managed WS feed + docs/HOST_QUICKSTART.md. (Iteration 6.)
- [x] **AWS deploy automation** — `deploy/aws/` Terraform (EC2 t4g/Tokyo, IAM-role
  S3 backups, cloud-init boots paper) + Litestream rendering in install.sh.
  (Iteration 6.)
