# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-
leverage *unblocked* thing. Add new findings as you discover them.

> 2026-06 overhaul: kill switch, honest measurement (funding attribution,
> per-agent Sharpe, paper fills), consolidated `hlbot run` engine, maker
> lifecycle v2, auto-promotion ladders + safeguards, moonshot profile,
> sweep harness. Superseded items moved to Done. New center of gravity:
> **make a strategy pass its gates on real evidence.**

## P0 — LIVE NOW: optimize the one confirmed edge, find the next (2026-06-14)

> Status as of 2026-06-14: **dislocation_reversion_v1 is the only confirmed
> edge** (G0 PASS at taker, robust through 3× slippage) and is **LIVE
> at live_small** on the dedicated account. Carry (xfund, funding_carry, S4)
> is empirically DEAD on HL — tested on real data, economically trivial after
> costs (<1%/yr); do not spend more effort there. liq_cascade's feed is dead.
> The mission now: make the live edge bigger and more reliable, and find the
> NEXT event-driven edge. Each loop iteration, take the top unblocked item.
>
> **DATA-RETENTION CAVEAT (measured 2026-06-14):** HL `candleSnapshot` serves
> only ~5000 candles/interval, so the 5m sweep's "90d" is really ~17.5d (15m
> ~52d, 1h ~208d). The dislocation edge is validated on ~17.5d / ~5d OOS / ~37
> OOS trades — real but THIN; do not read "90d" as 90d. Reports + a fetch-time
> WARNING now state the true window. Binance/Bybit (deep history + xvenue
> funding) are geo-blocked from the CI sandbox — those legs are host-only.

> **D1 re-sweep finding (2026-06-14):** re-ran `configs/sweeps/dislocation_reversion_v1.yaml`.
> 36 combos, **1 confirms** — `z=3/stop=0.02/hold=24` on the **8-coin** universe
> (OOS +3.0bps, sharpe +2.41, 37 trades), which is **exactly the deployed
> config**, so the top in-sample-ranked confirmed combo == live config → **no
> param change**. Same params on the 4-coin universe just miss (OOS +2.6) — the
> edge needs the broader universe for enough dislocations, so ensure live trades
> the 8-coin breadth (engine universe, not a dataclass default). Full-sample
> edge is robust (+7.7 → +5.7 → +3.7 bps through 1×/2×/3× slip); the marginality
> is the thin OOS split, not fragility to cost. Today's OOS (+3.0) is below a
> prior run's +5.0 — the ~17.5d window rolls daily, so the number is sample-
> sensitive; treat single re-confirms as noisy.

- [ ] **D1 — Keep dislocation_reversion honest & optimal (the live strategy).**
  It is LIVE; treat it with care. Each iteration: (a) read the newest
  `research/results/*dislocation*` from the nightly sweep; (b) if the sweep's
  **top IN-SAMPLE-ranked CONFIRMED** combo (the report already ranks by
  in-sample and only marks ✅ when it also clears OOS — never pick by the OOS
  column, that consumes the holdout and overfits) beats the deployed
  `z=3/stop=0.02/hold=24`, adopt it by **changing the agent dataclass
  DEFAULTS** in `agents/dislocation_reversion.py` (a tested code change), NOT
  `agent_overrides.json` — because `hlbot confirm` instantiates the agent with
  DEFAULTS, so only default-baked params are actually G0-validated; an override
  would inherit a stamp for a different config (the V3 hole below). (c) widen
  the sweep grid toward finer z/stop/hold and more universes to map the edge
  surface; (d) watch live exec quality (taker fill prices vs the 5m signal
  price — slippage
  is the live edge-killer). NEVER weaken its gates or caps; improvements are
  evidence-backed only.
- [ ] **D2 — More event-driven edges (the structural thesis).** Dislocation
  works because forced/emotional flow overshoots and reverts — edge density
  ~100× carry's. Spec + build siblings: (a) **funding-settlement snap** (fade
  the pre-settlement premium swing), (b) **new-listing day-1 reversion**
  (moonshot sleeve), (c) **OI-spike crowding-reversal** (S8 — needs OI
  history accrued forward via the WS feed). Each: spec → 5m/fine-candle
  backtest → confirm → let the supervisor promote. Backtestable ones first.
  > **D2a investigated (2026-06-14): NOT CONFIRMED.** The "settlement snap" is
  > really a funding-gated crowding fade (OI-free subset of S8): fade a 5m z
  > overshoot when |funding| ≥ ~15% APR (settlement timing irrelevant). Built as
  > `funding_crowding_fade_v1` (+ spec). Strong in-sample (+15bps, robust to 3×
  > slip) but **0/36 sweep combos clear G0** — walk-forward OOS fails on the thin
  > ~5d holdout (1–18 trades). NOT rostered; do not promote. Needs a real
  > forward-accrued 5m window (HL retains only ~17.5d) or OI (true S8) to retest.
  > Durable wins shipped: funding-history forward-pagination (HL caps at oldest
  > 500 rows — funding-as-signal was impossible before), 429 retry/backoff (a
  > rate-limited multi-coin sweep was silently reporting 0 trades), and
  > `funding_hourly` plumbed consistently into backtest + live views.
- [ ] **D3 — Signal expansion (where new edges hide).** Wire free signals into
  MarketView and test whether they sharpen dislocation entries or seed new
  agents: cross-venue funding (Binance/Bybit, S5), L2 book imbalance (we have
  the WS book), OI/positioning. Investigate-then-test; record dead ends.

## P0b — deferred infra/safety from the audit (do when it unblocks the above)

- [ ] **V1 — verify/rewire the liquidation feed** (host: does `liq_log.jsonl`
  accrue? HL's public trades may not carry the flag — find the real source).
- [ ] **V3 — make `hlbot confirm` params-aware + params_hash provenance.**
  confirm instantiates agents with `config={}` (defaults), so it does NOT
  validate `agent_overrides.json` params — a tuned override inherits a G0
  stamp for the wrong config. Fix: confirm/sweep load the same overrides the
  runner does (or take a `--params`), and stamp a `params_hash` into
  `confirmations` that `require_g0` matches against the deployed config.
  Until then D1 adopts params via dataclass defaults (which confirm DOES see),
  not overrides. Critical now that dislocation is live and ralph tunes it.
- [ ] **V6 — equity-floor flow adjustment** (deposits/withdrawals inflated the
  drawdown and tripped the kill on 2026-06-12; track net transfers).

## (archived) P0 — carry builds, now dead-ended by real data

> Kept for the record; do NOT work these — the 2026-06-13/14 real-history
> tests proved carry is economically trivial on HL. The dislocation edge
> superseded them.

- [B] **S4 — Spot↔perp baseline carry.** Spec:
  `docs/research/S4_spot_perp_carry.md`. Harvests the ever-present ~11% APR
  baseline market-neutral (no spike needed). Needs: spot order support in
  `exec/` (the Exchange client takes a spot pair name; extend
  `place_limit_order` asset/szDecimals resolution for spot), a spot/perp pair
  agent with leg-sequencing (perp leg only after spot leg confirmed) +
  basis-stop + unwind rule, spot candle fetch in `backtest-fetch`, and a sweep
  spec. _Blocked: confirmed economically trivial (<1%/yr) on 180d real data._
- [ ] **S8 — Crowding-reversal (the operator's sentiment edge, done +EV).**
  Spec: `docs/research/S8_crowding_reversal.md`. Fade multi-signal sentiment
  extremes (price-move + OI-spike + funding-extreme) with a hard stop. Needs:
  OI plumbed into MarketView from `metaAndAssetCtxs`, the agent + sweep spec.
  Note OI history isn't in the candle cache — phase 1 likely paper-soaks for
  real evidence rather than backtesting (accrue OI forward via the WS feed).
- [ ] **S5 — Cross-venue funding signal.** Spec:
  `docs/research/S5_xvenue_funding.md`. Now MORE valuable given the baseline
  finding: Binance/Bybit funding tells us when HL's *spike* is idiosyncratic
  (fade-able, feeds S8) vs market-wide. Phase-1 offline study first.

## P0b — review remediation (2026-06-12 four-track audit)

> A full product audit (execution, measurement, strategies, ops) found and
> fixed critical defects. These follow-ups are the highest-leverage items
> after the audit fixes (now merged).

- [ ] **V1 — Verify the liquidation feed on the host.** The WS `trades`
  handler reads a `liquidation` flag that HL's public schema may not carry,
  and the REST fallback endpoint likely doesn't exist — liq_cascade may have
  NEVER seen an event and `data/liq_log.jsonl` may be empty. Host: run
  `hlbot ws` through one volatile session and check the log accrues; if not,
  find the real source (HL docs: liquidations appear as trades with special
  `users`; or `userEvents`/explorer feeds) and rewire `ingest/ws.py:80`.
  Everything about liq_cascade and the moonshot sleeve is gated on this.
- [ ] **V2 — Rebuild liq_cascade as maker-resting REVERSION** per the quant
  review + `docs/research/E5_event_reactor.md`: fade the dislocation with
  resting bids below mid (the cascade fills you at maximum spread), |A−B|
  imbalance trigger (done), TP/SL swapped to positive skew. Spec first;
  calibrate from liq_log once V1 confirms data.
- [ ] **V3 — Evidence provenance (params_hash).** Stamp a config/params hash
  into `confirmations`, sweep results, and (cheaply) decisions; promotion's
  require_g0 should match the *currently deployed* hash. Today ralph can tune
  params and inherit weeks of old-params evidence (audit finding G1). Until
  then: any agent param change should bump the agent name (`_v2`) instead.
- [ ] **V4 — Window-boundary edge fix.** `edge_bps` pairs close-fill PnL with
  in-window notional only — trades straddling the window start inflate edge
  ~2x at some rolling looks. Proper fix: round-trip series bucketed by close
  time with matched entry notional (pairing logic exists in
  `scoring/attribution.py`). The persistence gate mitigates; this removes it.
- [ ] **V5 — Ralph privilege separation.** The loop runs as the trading user
  inside the live working tree (configs hot-reload into the engine mid-
  iteration) and can edit its own guardrail tests. Host: separate clone +
  user for the loop; CI: diff-check `tests/test_gate_minima.py`, `ops/`,
  `risk/`, `backtest/engine.py::CostModel` against main and fail on weakening.
- [ ] **V6 — Equity-floor flow adjustment.** The 75%-of-HWM kill is not
  deposit/withdrawal-adjusted (a >25% withdrawal trips it; deposits inflate
  HWM). Track net transfers (ledger endpoint) and adjust the HWM basis.
- [ ] **V7 — xfund long-leg reality.** The long leg (funding ≤ −threshold)
  almost never exists, so xfund runs short-only at half cap. Sweep a relaxed
  long-leg threshold (e.g. long the *least positive* funding names as the
  hedge) vs. accepting short-only with the imbalance cap — decide on data.
- [ ] **V8 — twap_mr_regime re-confirmation.** Signal windows are now unified
  (live = backtest = 60×1h); the agent is frozen `roster: paper` until a
  fresh G0 (with the honest maker-fill model) passes on the unified signal.

## P0b — confirm an edge on real history (host-side first)

- [ ] **R1 — First real sweep results.** Host: `hlbot-sweep.timer` (or manual
  `deploy/run-sweep.sh`) populates `research/results/`. Then: act on the
  evidence — fold the best confirmed combo into `configs/agent_overrides.json`
  (tightening-only) and have the host stamp `hlbot confirm --record`.
  _Blocked in the CI sandbox (no HL egress); host-only._
- [ ] **R2 — Diagnose if nothing confirms.** If carry doesn't clear G0 with
  maker costs over 180d: decompose (gross carry collected vs costs vs adverse
  price drift per leg) using the sweep JSON; write the autopsy to
  `docs/research/carry_autopsy.md`. Decide: tune (rank window, exit band,
  universe) or kill the class and pull the next spec.
- [ ] **R3 — Paper-soak verification.** After ≥3 days of `hlbot run` paper
  operation: assert paper_fills/paper_funding accrue for every roster agent,
  scorecards show non-None sharpe, and `goal_evaluations` records promotion
  blockers (min-days/G0) instead of silence. Fix anything dishonest.

- [x] **B6 — Per-agent funding attribution.** Done: `scoring/attribution.py`
  replays fills into position timelines and attributes each funding payment to
  the holder (proportional split); wired into `score_agent` so funding is in
  each agent's net. (REVIEW C4; iteration 7.)
- [x] **B7 — Per-agent equity curves + Sharpe/DD.** Done: per-agent Sharpe from
  daily net PnL (fills + attributed funding) and `max_drawdown_usd` on every
  Scorecard; curve math shared via `scoring/curves.py` (backtester imports the
  same code). (REVIEW C5; iteration 7.)
- [x] **B8 — Record actual fill price.** Done: `femr_tick` now logs the confirmed
  `res.avg_px`/`res.filled_sz` on fill so stops/TPs key off the real entry. (M1.)
- [x] **B9 — fills→positions replay.** Done:
  `attribution.replay_positions_table` rebuilds `positions` from fills
  (add/reduce/flip), runs on every `hlbot ingest`. (REVIEW M2; iteration 7.)

## P1 — execution quality (every bp saved is pure edge)

- [ ] **E1 — Maker fill telemetry.** From `maker_orders` + fills: fill rate,
  median time-to-fill, reprice count, taker-fallback rate per agent/coin/24h;
  surface in `hlbot report` + health alert when fill rate < 30% (P7 spec).
- [ ] **E2 — Tune MakerConfig from data.** Once E1 has a week of live_small
  data: reprice_bps / min_requote_s / max_rest_s per coin-liquidity bucket.
  Tightening-only on risk; document evidence in PROGRESS.md.
- [ ] **E3 — userFills WS subscription** for instant maker-fill detection
  (today: fill detection waits for REST ingest each 5 min leg).
- [ ] **E4 — Reduce-only maker exits.** Normal (non-stop) exits currently
  cross as takers; route them through the lifecycle's reduce-only post-only
  path with `exit` urgency once E1 proves fills come fast enough.

- [x] **B10 — WebSocket market view.** Done: `ingest/ws.py` MarketState +
  `hlbot ws` service writes a snapshot; live tick overlays it (HLBOT_WS_SNAPSHOT)
  for sub-second mids, L2 book_top, and a real liquidations feed (fixes C6), with
  REST fallback. Next: userFills WS for instant maker-fill detection. (Iteration 5.)
- [x] **B-book — Book-aware maker pricing.** Done: `exec/maker.py::maker_price`
  joins the touch from the WS book (REST-mid fallback); used by the router for
  every maker entry. (Iteration 7.)
- [x] **B11 — Retire or feed liq_cascade.** Resolved: the agent stays on the
  roster (so its stops/exits keep managing any held position) but is entry-dead
  by construction without a WS snapshot — its only real liquidation source. The
  live tick prints an explicit notice when HLBOT_WS_SNAPSHOT is unset.
  (REVIEW C6; iteration 7.)
- [x] **B12 — Consolidate execution paths.** Done: all live order routing goes
  through `exec/router.py::execute_decisions` (per-agent maker/taker entries,
  taker exits, gates, fill-confirmed logging) — unit-tested with a fake
  exchange. (REVIEW M3; iteration 7.)
- [ ] **B13 — Move hardcoded trader address to config.** (REVIEW M6.)
- [x] **B14 — Go-live runbook in-repo.** `docs/GO_LIVE.md`: gated checklist,
  secrets/env, promote/kill-switch/rollback, monitoring. (REVIEW D3.)
- [ ] **B14a — Deploy automation.** Codify the EC2/systemd/Hermes cron + DB sync
  (without secrets) so the live loop is reproducible from the repo.

## P2 — strategy pipeline

- [ ] **S1 — Implement specs from `docs/research/`** as they land (agent +
  factory + YAML contract + sweep spec + tests). None pending yet.
- [ ] **S2 — liq_cascade calibration from `data/liq_log.jsonl`.** After ≥2
  weeks of WS logging: distribution of cascade sizes, post-cascade drift by
  horizon; set `min_liq_notional_usd` and hold windows from data; build a
  replay backtest over the log so the strategy can earn a G0-equivalent stamp.
- [ ] **S3 — Funding-rate persistence study.** Is top-K funding rank sticky
  enough that rotation costs don't eat the carry? (Feeds xfund exit-band
  tuning; pure research over cached funding history.)

## P3 — capital formation (Path C)

- [x] **B15 — Public-grade track-record export.** Done: `reports/track_record.py`
  + `hlbot track-record` → track_record.{json,md,svg,html} (equity curve chart,
  Sharpe/DD/edge, per-agent incl. attributed funding). (Iterations 2, 7.)
- [x] **B16 — Hyperliquid vault evaluation.** Done as spec:
  `docs/MONETIZATION.md` — leader earns 10% of depositor profit, ≥5% own stake,
  10k USDC creation; hard-gated behind G3. Adapter code deferred until G3.
- [x] **B-MON — Monetization levers doc + builder-code plumbing.** Done:
  `docs/MONETIZATION.md` (fee stack, airdrop posture, vault, referral) and
  optional builder field on all orders (`exec/orders.py::_builder_info`,
  env-gated, off by default). (Iteration 7.)

## Done (overhaul, 2026-06)

- [x] **Kill switch** — sticky `data/KILL`, `hlbot kill/resume`, enforced at
  cycle start + before every placement; equity-floor (75% of 30d HWM) and
  account daily-loss breaches trip it automatically.
- [x] **B6/B7/B9 — honest measurement** — fills→positions replay, per-agent
  funding attribution (residual reconciles to exchange), per-agent synthetic
  equity Sharpe/maxDD.
- [x] **Paper fills** — simulator (conservative maker cross rule + hourly
  funding accrual) makes paper performance scoreable; promotion gates can
  finally fire from paper.
- [x] **B12 — consolidated engine** — `hlbot run` long-running service
  (hlbot-run.service replaces the 5-min tick timer); per-agent cooldowns.
- [x] **B-book / maker lifecycle v2** — `exec/lifecycle.py` state machine:
  quote at touch from WS L2, reprice on drift, partials, expiry, exit
  escalation to taker; `maker_orders` table.
- [x] **Auto-promotion** — promotion ladders staged on DB mode (bug fix),
  min_days_in_mode, require_g0 (`hlbot confirm --record`), paper/live metric
  sources, mode sizing (live_small tiny), order-rate limits;
  `tests/test_gate_minima.py` makes gate-weakening fail CI.
- [x] **B11 — liq_cascade fed** — WS liq feed plumbed; events persisted to
  `data/liq_log.jsonl` for calibration; agent incubating in paper.
- [x] **B17 — moonshot sleeve** — `--profile moonshot`: own sub-account/DB/
  KILL/configs/wallet/systemd/Litestream; rules in docs/MOONSHOT.md.
- [x] **Sweep harness** — `hlbot sweep` + configs/sweeps/ + nightly
  hlbot-sweep.timer committing ranked results to research/results/.
- [x] **Roster surgery** — twap_mr_v1 & basis_v1 retired; carry agents in the
  roster (they previously could not trade at all).

## Done (pre-overhaul iterations 0–6)

- [x] B0 backtest harness · B1a offline cache · B2/B2b maker primitives +
  lifecycle v1 · B3 twap_mr_regime · B4 carry strategies · B5 confirm harness
  · B8 real fill px · B10 WS market view · B13 env trader address · B14
  GO_LIVE runbook · B14a deploy automation · B15 track-record export ·
  B-INFRA docs · AWS Terraform deploy · health/doctor ops.
