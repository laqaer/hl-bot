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

## P0 — confirm an edge on real history (host-side first)

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

- [ ] **B16 — Hyperliquid vault evaluation.** Spike: requirements, fees, risks
  of running an HL vault; gate behind a real track record (G3).
- [ ] **B15b — Track-record chart export** (equity-curve PNG/SVG for the
  public track record).

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
