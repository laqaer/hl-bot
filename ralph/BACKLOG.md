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
- [ ] **B1a — Offline history cache.** Add `hlbot backtest-fetch` to pull candle/
  funding history to a local parquet/sqlite cache under `data/` (gitignored), so
  backtests are reproducible and runnable without live network each time.
- [ ] **B2 — Maker (post-only) execution.** Add a limit/ALO order path in
  `exec/orders.py` and a `Decision`/agent option to prefer maker. Default exits
  may stay taker for urgency. Backtest the passive strategies as maker first.
- [ ] **B3 — `twap_mr_regime_v1`.** New agent that consults
  `research/candidates.py::regime_allows_fade` before placing a fade. Backtest vs
  baseline TWAP on trending vs choppy history; promote only if net-of-cost edge
  improves. (Wires REVIEW C3.)
- [ ] **B4 — Maker carry strategy.** A hold-to-collect funding strategy that
  enters maker, holds while |funding| stays extreme, and only collects (no tight
  TP/SL churn). Backtest with funding folded in. (Fixes FEMR's economics.)
- [ ] **B5 — Walk-forward + cost-stress harness.** Add a helper that splits
  frames into in/out-of-sample windows and re-runs the cost model at 1×/2×/3×
  slippage, so "edge" must survive realistic stress before G0.

## P1 — honest measurement (so the supervisor can trust itself)

- [ ] **B6 — Per-agent funding attribution.** Map `funding_payments` to the agent
  holding the position at funding time (via cloid→position replay), so
  `scoring.metrics` includes funding in each agent's net. (REVIEW C4.)
- [ ] **B7 — Per-agent equity curves + Sharpe/DD.** Reuse the backtest
  equity-curve math (`engine._curve_stats`) to compute per-agent Sharpe/maxDD
  from fills, so `funding_arb_v1.yaml`'s sharpe gate can actually evaluate.
  Then standardize all goal configs. (REVIEW C5.)
- [ ] **B8 — Record actual fill price.** In `cli/main.py::femr_tick`, log the
  confirmed `res.avg_px` (not pre-trade mid) on fill, so stops/TPs key off the
  real entry. (REVIEW M1.)
- [ ] **B9 — fills→positions replay.** Populate the unused `positions` table from
  fills so attribution survives partial fills/manual interference. (REVIEW M2.)

## P2 — cadence, structure, devops

- [ ] **B10 — WebSocket market view** for fast strategies (sub-minute), keeping
  the cron only for low-frequency carry. (REVIEW C7; README roadmap.)
- [ ] **B11 — Retire or feed liq_cascade.** Source real liquidation data (WS
  trades liquidation flag) or disable the agent until it can be fed. (REVIEW C6.)
- [ ] **B12 — Consolidate execution paths.** `runtime.run_tick` vs `femr_tick`
  duplicate logic; unify so the safe wrapper is what live uses. (REVIEW M3.)
- [ ] **B13 — Move hardcoded trader address to config.** (REVIEW M6.)
- [ ] **B14 — Deploy/runbook in-repo.** Document the EC2/systemd/Hermes cron setup
  (without secrets) so the live loop is reproducible. (REVIEW D3.)

## P3 — capital formation (Path C)

- [ ] **B15 — Public-grade track-record export.** Equity curve + Sharpe/DD/edge
  to a shareable artifact (CSV/JSON + chart) for vault/depositor due diligence.
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
