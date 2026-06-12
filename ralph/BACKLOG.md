# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-leverage
*unblocked* thing. Add new findings as you discover them.

## P0 — find an edge (the whole point)

- [B] **B1 — Quantify the taker tax on every agent.** Run
  `hlbot backtest --agent <a> --compare` over ≥90d for twap_mr/femr; record the
  taker vs maker net/edge in `PROGRESS.md`. _Blocked in CI sandbox: outbound to
  api.hyperliquid.xyz is 403. Run where HL history is reachable, or add B1a._
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
- [ ] **B4-RUN — Confirm carry strategies on real history.** Run `hlbot confirm
  --agent xfund_carry_v1 --prefer maker` (and funding_carry_v1) on a net host;
  promote to the paper roster if confirmed. Blocked by B1 network.

## P1 — honest measurement (so the supervisor can trust itself)

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

## P2 — cadence, structure, devops

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
- [x] **B13 — Trader address from env only.** Fully resolved: env vars win AND the
  legacy hardcoded fallback is gone — `require_trader_address()` fails fast when
  neither HL_TRADER_ADDRESS nor HL_ADDRESS is set. (REVIEW M6; iteration 7.)
- [x] **B14 — Go-live runbook in-repo.** `docs/GO_LIVE.md`: gated checklist,
  secrets/env, promote/kill-switch/rollback, monitoring. (REVIEW D3.)
- [x] **B14a — Deploy automation.** Done in iteration 5 (`deploy/`: install.sh,
  systemd units, Litestream, loop service, run-tick); the legacy Hermes scp sync
  stays out-of-repo by design (secrets).

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
