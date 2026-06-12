# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-leverage
*unblocked* thing. Add new findings as you discover them.

## P0 — find an edge (the whole point)

- [x] **B1 — Quantify the taker tax on every agent.** Done (Iteration 20, network
  reachable on this host). 90d 1h, BTC/ETH/SOL/HYPE: twap_mr_v1 taker −10.0bps →
  maker −4.6bps (tax ≈5.4bps); twap_mr_regime_v1 −9.8→−4.5 (regime filter nearly
  inert at 1h on majors); femr_v1 0 trades on majors (funding too small). Numbers
  in `PROGRESS.md`. _Was blocked on network; now unblocked here._
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
- [~] **B4-RUN — Confirm carry strategies on real history.** Run on a 10-coin
  liquid-alt universe incl. ZEC (Iteration 20, network up). **NOT confirmed (FAIL
  G0):** xfund_carry_v1 maker −4.3bps full-sample but OOS-maker +3.4bps/+0.60sh
  (52 trades) — a faint recent-regime signal, in-sample −43.7bps; funding_carry_v1
  maker −111bps (single-name carry run over by price on volatile alts); femr_v1
  maker −27.9bps. Numbers in `PROGRESS.md`. **Prerequisite found+fixed:** HL
  `fundingHistory` 500-row cap silently truncated >20d windows → all prior carry
  backtests were invalid (saw only oldest ~20d of carried-forward funding); fixed
  via `paginate_by_time`. **Next:** xfund is closest — try wider universe / longer
  hold / tighter entry; do NOT promote (still negative).

- [x] **B1b — Paginate candle fetch too.** Done (Iter 21): `fetch_candles` now
  walks the candleSnapshot ~5000-row per-call cap via `paginate_by_time(time_key=
  "t", page_limit=CANDLE_PAGE_LIMIT)`, so fine intervals (1m/5m) over long windows
  no longer silently truncate the recent bars the way `fundingHistory` did.
  Unit-tested offline (fake httpx, shrunk cap).
- [~] **B1c — Edge hunt on corrected funding.** Tooling done: `backtest`/`confirm`
  now take `--config '{json}'` overrides (`parse_agent_config` + `_backtest_factories`,
  tested) so xfund params sweep without code edits. **Two hypotheses pruned (Iter 22,
  numbers in PROGRESS):** (1) *tighter entry* makes xfund WORSE not better
  (2bp/hr → −40.5bps maker / 18 trades / 33% win; ≥3bp/hr → 0 trades, funding caps
  ~2.7bp/hr); (2) *wider universe* (10→20 liquid alts) also WORSE (−4.3 → −12.1bps
  maker, OOS −23.6bps) — the Iter-20 narrow-10-coin OOS +3.4bps was a sample
  artifact, not robust. Root cause: high-|funding| alts are volatile *because*
  funding is extreme, so price variance buries the carry whether you concentrate
  OR diversify. `top_k>2` inert (eligible legs/side cap out). **Best config in book
  is still the loosest baseline at −4.3bps maker — negative.** **Third hypothesis
  pruned (Iter 23):** *cut the rotation churn* via `hold_while_eligible` (hold a leg
  while its funding stays eligible+correct-side instead of exiting on rank rotation)
  cut trades 62→36 as designed but made it WORSE (−4.3 → −17.6bps maker / −1.57
  net). Root cause: the churn wasn't waste — it concentrates the book into the
  *highest*-funding names; holding rank-rotated lower-funding legs collects less
  carry while still eating price variance over a longer hold. The config lever is
  kept (default off, tested) so the dead end isn't re-explored. Remaining to try:
  funding-decile *neutralised-by-beta* cross-section (dollar-neutral ≠ market-neutral
  when shorts are higher-beta alts), or a different (lower) cadence so funding accrual
  outweighs per-bar price noise. Evidence-gated; nothing promoted.

## P1 — honest measurement (so the supervisor can trust itself)

- [x] **B6/B7 — Per-agent funding attribution + Sharpe.** Done: funding split to
  the agent holding the coin at funding time (scoring includes it in net/edge);
  per-agent Sharpe from daily PnL so sharpe-gates evaluate. Tested. (Iteration 7.)
- [x] **B6old — (superseded by B6/B7/B9b)** Map `funding_payments` to the agent
  holding the position at funding time. Done by B9b: `_agent_funding_payments` now
  splits each payment by *signed held size* from the fills replay (B9), falling
  back to decision-log equal-split only when no fills exist. (REVIEW C4. Iter 11.)
- [x] **B7 — Per-agent equity curves + Sharpe/DD.** Done: per-agent Sharpe from
  daily PnL (Iter 7) + per-agent fractional `max_drawdown`/`calmar` from a
  `capital`-based synthetic equity curve (`_daily_pnl_drawdown`), threaded via
  `score_agent(capital_base=)` and a new `AgentGoals.capital` field — so
  `funding_arb_v1.yaml`'s drawdown guardrail can finally fire (it was permanently
  N/A). Configs that want fractional gates set `capital:`. (REVIEW C5. Iter 9.)
- [x] **B8 — Record actual fill price.** Done: `femr_tick` now logs the confirmed
  `res.avg_px`/`res.filled_sz` on fill so stops/TPs key off the real entry. (M1.)
- [x] **B9 — fills→positions replay.** Done: `scoring/positions.py`
  (`replay_positions` pure state machine + `rebuild_positions(conn)`) populates
  the `positions` table from fills (net_sz, size-weighted avg_entry, accumulated
  realized_pnl/fees), surviving partial fills/flips/manual interference; wired
  into `hlbot ingest` and exposed via `hlbot positions`. (REVIEW M2. Iter 10.)

## P2 — cadence, structure, devops

- [x] **B10 — WebSocket market view.** Done: `ingest/ws.py` MarketState +
  `hlbot ws` service writes a snapshot; live tick overlays it (HLBOT_WS_SNAPSHOT)
  for sub-second mids, L2 book_top, and a real liquidations feed (fixes C6), with
  REST fallback. (Iteration 5.)
- [x] **B10b — userFills WS for instant maker-fill detection.** Done: WS
  `userFills` channel captured into `MarketState.user_fills` + persisted in the
  snapshot; `ingest_ws_user_fills` upserts them via a shared `upsert_fill`
  (deduped by (hash,tid) against REST), and `femr_tick --execution maker` folds
  them in before `reconcile_maker_fills` so a quote that filled seconds ago is
  reconciled THIS tick, not next REST poll (fixes the C7 cadence gap for makers).
  `run_ws(user_address=)` subscribes. Unit-tested offline. (Iteration 19.)
- [x] **B-book — Book-aware maker pricing.** Done: `maker_limit_price` joins the
  near touch (best bid/ask) from the WS L2 book; live maker entries price off
  `view.book_top` (fallback mid, never cross). Tested. (Iteration 8.)
- [x] **B11 — Retire or feed liq_cascade.** Done: retired the phantom REST
  `{"type":"liquidations"}` call (always returned nothing) and added a
  `liquidations_feed` flag. liq_cascade now opens new positions ONLY when a real
  feed is present (WS trades liquidation flag / backtest), else it emits an
  explicit "feed unavailable" hold; exits still run so positions aren't stranded.
  (REVIEW C6. Iteration 12.)
- [~] **B12 — Consolidate execution paths.** `runtime.run_tick` vs `femr_tick`
  duplicate logic; unify so the safe wrapper is what live uses. (REVIEW M3.)
  **Done so far:** (a) the live order-placement loop is extracted from `femr_tick`
  into `runtime.execute_decisions` (pure of presentation, returns `ExecEvent`s)
  and unit-tested with a fake exchange (M3/D2). (b) the decision-gathering loop is
  extracted into `runtime.gather_decisions` — both `run_tick` (paper) and
  `femr_tick` (live) now share it, and `femr_tick` gained the per-agent `decide()`
  crash isolation it lacked (a broken agent no longer aborts risk-reducing
  flattens). Unit-tested. (Iteration 14.)
  (c) the inlined clearinghouse parse + per-agent reconcile loop are extracted into
  tested `runtime.positions_from_clearinghouse` + `runtime.reconcile_agents`,
  removing ~25 lines of untested CLI code. (Iteration 15.)
  (d) the allocator cap resolution loop (MetaAllocator → resolve_agent_caps →
  per-agent cfg mutation) is extracted into tested `runtime.apply_allocator_caps`
  returning `AllocatorCaps`, removing ~30 lines + 2 now-dead imports from the CLI.
  (Iteration 16.)
  (e) the WS snapshot overlay (additive merge of fresh mids/funding/book_top +
  real liquidations feed) is extracted into tested `runtime.overlay_ws_snapshot`
  returning `WsOverlay`; the CLI keeps only the env-read + file load + print.
  (Iteration 17.)
  (f) the bot-owned/manual position partition (the per-agent `bot_owned_coins`
  union + manual-coin split) is extracted into tested
  `runtime.classify_position_ownership` returning `PositionOwnership`; the CLI
  keeps only the femr `live_positions` line + display. (Iteration 18.)
  **Remaining:** fold the rest of the `femr_tick` preamble (clearinghouse fetch →
  risk-cap → view enrich) into a reusable harness so `run_tick` and `femr_tick`
  share one path end-to-end.
- [x] **B13 — Move hardcoded trader address to config.** Done (Iter 6, M6):
  `exec/orders.py` and `scripts/daily_scorecard.py` both resolve the trader address
  from `HL_TRADER_ADDRESS`/`HL_ADDRESS` env, legacy default only as fallback.
- [x] **B14 — Go-live runbook in-repo.** `docs/GO_LIVE.md`: gated checklist,
  secrets/env, promote/kill-switch/rollback, monitoring. (REVIEW D3.)
- [ ] **B14a — Deploy automation.** Codify the EC2/systemd/Hermes cron + DB sync
  (without secrets) so the live loop is reproducible from the repo.

## P0.5 — prove the edge at scale (now that twap_mr_v1 is live + profitable)

- [ ] **B-SCALE — Scale twap_mr_v1 as it earns.** Let the 5×/1× risk rule +
  MetaAllocator grow size at each gate; bump caps deliberately, never to chase losses.
  This builds the track record on meaningful size.
- [ ] **B4-RUN — Confirm carry strategies on real history.** `hlbot confirm --agent
  xfund_carry_v1 --prefer maker` (and funding_carry_v1) on the live box; promote any
  that pass G0 into the live roster.
- [ ] **B-UNIV — Widen the carry universe** to 50–100 coins so cross-sectional carry
  has more to rank (config + universe fetch).
- [x] **B15c — Track-record CHART export.** Done: `reports/track_record.to_html`
  emits a self-contained HTML page with an inline SVG equity curve + per-agent
  table; `hlbot track-record` → `track_record.html`. Tested. (Strategy-review iter.)

## P0.6 — strategy experiments (validate on REAL data before flipping live)

See `docs/STRATEGY_REVIEW.md`. `twap_mr_v1` levers ship default-OFF; the loop A/Bs
each on ≥90d real history (`hlbot confirm` / `hlbot backtest --config`) before flip.

- [x] **B-REGIME — A/B `regime_filter`** on twap_mr. Done at 1h cadence (Iter 27,
  90d 10-coin, numbers in PROGRESS): clear dose-response — best config
  (`regime_min_move_pct=0.015, regime_min_consistency=0.55`) improves maker edge
  −5.0→−3.0bps, halves maxDD (−21.7%→−9.9%), cuts trades 30% — but never flips
  positive (OOS −6.0bps, G0 FAIL). Defaults (0.03/0.65) are inert at 1h. Verdict:
  the lever's direction is validated, keep default OFF, do NOT flip live until it
  A/Bs positive at live-like cadence (B-CAD; blocked on retention → B-HIST).
- [x] **B-HIST — Rolling fine-candle accumulator (prereq for B-CAD).** Done
  (Iter 28): `backtest/store.py` (merge-by-t, fresh-wins, atomic gz writes) +
  `hlbot harvest-candles` (10-coin universe × 1m/5m/15m, per-pair error
  isolation) + `hlbot-harvest.timer` (hourly, enabled by install.sh AND
  self-enabled by update.sh — auto-update previously copied new timers without
  enabling them). Validated on real API: 30/30 pairs, full retention captured
  (3.5d@1m / 17.4d@5m / 52.1d@15m, 3.6MB), incremental top-up proven.
- [x] **B-HIST2 — Backtest from the store.** Done (Iter 30): `--source store` on
  `hlbot backtest`/`confirm` via `store.frames_from_store` (candles from
  `data/candle_store/`, funding still API-fetched, seeded 2h before the first
  bar; `--days 0` = everything stored; backtest-only `--no-funding` for offline
  price-only runs) + per-coin `StoreCoverage` gap report so a harvester outage
  can't silently pass a holey sample as a full one. Validated on the real store
  (10 coins × 1m, 0 bars missing): maker +4.6bps, confirm **G0 PASS** —
  matches the Iter-29 API-sourced run on the ~half-day-shifted window.
- [ ] **B-G014 — Multi-week exact-replica G0 from the store. ← TOP PRIORITY once
  store 1m span ≥ ~14d** (harvester started 2026-06-12 → ETA ~2026-06-26; check
  spans via `hlbot harvest-candles`). Run `hlbot confirm --agent twap_mr_v1
  --coins ADA,...,ZEC --interval 1m --vwap-window 60 --days 0 --source store
  --prefer maker`. A PASS on ≥2 weeks is the durable-edge evidence B-MAKER-LIVE
  and B-SCALE are waiting on; a FAIL means the Iter-29 PASS was the recent
  pocket, not the strategy. **Run a second arm with `--config
  '{"stop_loss_pct":0.03}'`** (B-EXIT's robust lever, Iter 31): if it passes
  AND beats baseline on the multi-week sample, propose the default flip to the
  operator (live strategy change — not loop-flippable).
- [x] **B-CAD — A/B levers at live-like cadence.** Done (Iter 29, numbers in
  PROGRESS): `--vwap-window` exposed in backtest/confirm/backtest-fetch (window
  keys the cache when ≠60). Cadence explains the backtest/live divergence: maker
  edge −5.0bps (1h) → −1.5bps (5m w=12 and 15m w=4 proxies) → **+5.4bps at the
  exact live config (1m w=60, 3.5d, 844 trades, G0 PASS — sample-limited)**.
  Taker at 1m = −0.0bps: the spread tax eats the entire edge → filed B-MAKER-LIVE.
  Lever verdicts at live-like cadence: regime_filter helps at 5m (−1.5→−0.8
  maker) but is inert at 15m and unneeded at 1m; size_by_signal dampens losses
  where edge<0 but cut net profit 42% at 1m where edge>0. **Both stay default
  OFF; no live change.**
- [B] **B-MAKER-LIVE — Route live entries through maker execution.** *Human-gated
  (live change).* The 1m exact-replica backtest: maker +5.4bps vs taker −0.0bps —
  the taker tax is the whole edge at live cadence. The machinery exists and is
  tested (`--execution maker`, B2b + B10b instant fill detection). Operator: set
  `HLBOT_TICK_ARGS="--live --execution maker"` in /etc/hl-bot/env per
  deploy/README.md §4 and watch the first session at current size. Risk: maker
  entries can miss fills in fast moves (fewer trades, not losses; exits stay taker).
- [x] **B-SIZE — A/B `size_by_signal`.** Done as part of Iter 29 (see B-CAD):
  helps only when the strategy is losing; at the live config it cut profit 42%
  (winners get sized down too). Keep OFF. The vol-targeting variant is pruned
  unless drawdown control becomes the binding problem.
- [x] **B-EXIT — sweep `sigma_exit`/`stop_loss_pct`/`max_hold_hours`.** Done
  (Iter 31, all from cached real data, numbers in PROGRESS). One robust lever
  found: **`stop_loss_pct` 0.015→0.03 improves every live-like sample** —
  1m/3.5d maker +5.4→+6.1bps (G0 PASS, taker flips −0.0→+0.5), 5m/17d
  −1.5→−0.2 (maxDD −15.2→−12.2%), 15m/52d −1.5→−1.0; only 1h/90d disagrees
  (not the live strategy). Mechanism: a 1.5% stop at fine cadence converts
  transient wicks into realized losses before the reversion exit can pay.
  **Not flipped (live strategy change; 90d-at-live-cadence bar unmet)** —
  second confirm arm added to B-G014. `sigma_exit` tightening (0.25/0.1) is a
  fair-weather lever: big gains on the recent 3.5d pocket at every cadence but
  worse on 17d — regime, not cadence; keep 0.5 (0.0 collapses the strategy,
  proving the reversion exit is the profit engine). `max_hold_hours` inert at
  1m and 5m (reversion/stop always fires first); keep 4.
- [x] **B-FUND — funding-aware fade suppression.** Done (Iter 32, numbers in
  PROGRESS): `funding_filter` lever ships default-OFF (`funding_allows_fade`,
  hourly-rate threshold) + a units fix it surfaced — backtest `view.funding` was
  the per-bar-scaled rate (60× too small at 1m vs live); engine now feeds the
  hourly series (`Frame.funding_hourly`, legacy caches backfilled). **Verdict:
  pruned at live cadence** — at the exact live config the filter HURTS at every
  threshold (maker +5.4→+4.4..+4.6bps; the adverse-funding fades were the
  profitable ones — extreme funding against a fade marks the crowded positioning
  the reversion harvests). Helps only on 5m/17d (−1.5→−0.5, clean dose-response,
  maxDD −15.2→−10.7%) but still G0 FAIL (OOS +1.6bps); inert at 15m/1h. Same
  window flips sign across cadence (5m/3d helps, 1m/3.5d hurts) → keep OFF.
- [ ] **B-WIN — VWAP window study** (1h vs 2–4h vs volume-weighted σ).
- [ ] **B-EDGE2 — hunt a second, low-correlation edge** (carry is pruned) so the
  book isn't single-strategy before raising AUM.

## P3 — capital formation (see docs/CAPITAL.md)

- [x] **B15 — Public-grade track-record export.** Done: `reports/track_record.py`
  + `hlbot track-record` → track_record.{json,md}. Chart export = B15c above.
- [x] **B16 — Hyperliquid vault evaluation.** Researched (docs/CAPITAL.md): ~10%
  profit share, ≥5% leader TVL, ~1d depositor lockup, API-wallet-compatible. **Verify
  creation fee in current HL docs before launch.** Gate behind G3 track record.
- [ ] **B16b — Vault launch checklist + bot retargeting.** Steps to point
  `HL_TRADER_ADDRESS` at a vault sub-account once G3 is met.
- [ ] **B-PROP — Prop/funded eval prep.** Checklist for Hypernova/Propr/Velotrade
  (HL-native, API-friendly); run the same guardrailed strategy through an eval for
  additive $100–200k. (docs/CAPITAL.md Track B.)
- [ ] **B17 — Moonshot sleeve spec.** Design the ring-fenced, loss-bounded sleeve
  (separate sub-account, hard cap, defined max loss). Spec only; no live. (CAPITAL.md
  Track D.)

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
- [x] **B-PERF — linear-time `build_frames`.** Per-frame prefix scans (bars-so-far,
  funding sweep, 1440-bar vol sum) made fine-interval backtests quadratic; replaced
  with per-coin cursors + volume prefix-sums, equivalence-tested against the old
  logic verbatim. 90d×5m×2-coin build: 0.16s. (Iteration 27.)
