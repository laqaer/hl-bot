# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-leverage
*unblocked* thing. Add new findings as you discover them.

## P0 — find an edge (the whole point)

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
- [ ] **B-femr-regime — femr is dormant on majors; retire or repurpose.** femr's
  130%-APR entry never trips on liquid coins (B1). B1-alt now shows funding *carry*
  has no edge even where funding is high, so widening femr (also funding-driven) to
  alts is unlikely to help — the honest move is to **retire it from the live roster**
  until there's a universe+variant with a demonstrated G0 PASS. No live change either way.
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
