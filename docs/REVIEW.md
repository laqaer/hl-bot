# hl-bot — comprehensive review (2026-06-08)

A full read of every module. The engineering *spine* (accounting, safety,
supervision) is genuinely good. The *alpha* (the strategies) is not there yet,
and — critically — there was no way to find out before risking real money.
This review is the input to [`ROADMAP_TO_1M.md`](ROADMAP_TO_1M.md) and the
prioritized [`../ralph/BACKLOG.md`](../ralph/BACKLOG.md).

## TL;DR

| Area | Verdict |
|---|---|
| Architecture (3-layer: ground-truth → scoring → supervisor) | **Strong.** Clean, auditable, testable. |
| Safety (paper default, guardrails, cooldowns, reconciliation, fill verification) | **Strong.** Best part of the repo. |
| Strategy edge | **Negative.** Every live agent is bleeding after costs. |
| Execution | **Root cause of the bleed.** 100% taker market orders at small size. |
| Research capability | **Was the #1 gap → now addressed** (backtest harness added this session). |
| DevOps / CI | **Was red** (ruff) → fixed this session. |

## What's good (keep it)

1. **Cloid attribution** (`agents/cloid.py`) — packing the agent id into the
   client order id is a clean, robust way to attribute exchange fills back to a
   logical agent. Works for live and reconciliation.
2. **Ground-truth accounting** (`db/schema.py`, `ingest/hyperliquid.py`) — PnL
   is reconciled from exchange `userFills`, never invented internally. Right call.
3. **Order safety** (`exec/orders.py`) — inspects `statuses[].filled` (no phantom
   "ok"), retries with backoff, per-coin cooldown, **position reconciliation**
   against live state, size rounding to `szDecimals`, 0600 key-file permission
   check. This is production-grade defensive code.
4. **Supervisor semantics** (`supervisor/goals.py`) — N/A (missing metric) never
   triggers an action; risk controls dominate promotion. Subtle and correct.
5. **Risk scaling** (`risk/scaling.py`, `risk/allocation.py`) — portfolio-aware
   5×-total / 1×-per-position notional caps, layered with per-agent configured
   caps. Pure and unit-tested.
6. **Research hygiene** (`research/strategy_health.py`) — outlier-stripped
   "core edge" (excludes the single best coin) so one lucky coin can't make a
   bleeding book look healthy; only ever proposes *risk-reducing* changes.
7. **Test coverage** — 47 tests over the risk/supervisor/health/backtest logic.

## Critical findings (ranked by impact on the goal)

### C1 — 100% taker execution is the structural bleed *(highest impact)*
Every agent emits `place`/`flatten` that the executor turns into
`exchange.market_open` / `market_close` — i.e. **taker market orders**. For
mean-reversion, funding capture, and basis convergence, the *entire* edge is
often smaller than the spread + taker fee you pay to get in and out (~4.5 bps
fee + ~2 bps slippage ≈ **~13 bps round trip**). FEMR's measured 7d core edge is
**−51 bps**; TWAP's ~**−26 bps**. These are consistent with "a flat-to-slightly-positive
signal minus a fat taker tax."
*Fix:* maker/post-only limit execution for the passive strategies; reserve taker
for genuinely urgent exits. The new backtest harness has a `maker` flag
precisely to quantify this — run `hlbot backtest --compare` and read the
taker→maker gap.

### C2 — No backtest / simulation *(fixed this session)*
Previously the only "research" was post-hoc analysis of live losses plus an LLM
auto-tuner nudging σ by ±20%. You cannot discover a scalable edge by losing real
money $20 at a time. **Added `src/hl_bot/backtest/`**: a replay engine that drives
the real `decide()` over historical frames with an explicit cost+funding model
and scores via the production `score_agent`. This unblocks all trading research.

### C3 — TWAP fades into trends; the fix exists but isn't wired
`research/candidates.py::regime_allows_fade` is exactly the filter that stops the
"fade a breakout → lose → fade again" loop. It is **paper-only and never consulted
by the live `TwapMrAgent`**. The live agent keeps fading strong trends.
*Fix:* build a `twap_mr_regime_v1` that consults the filter, backtest it vs
baseline, promote only on evidence.

### C4 — Funding PnL is invisible to FEMR's scorecard
`scoring/metrics.py` only adds funding to the synthetic `_account` agent; every
real agent gets `funding_pnl = 0`. FEMR is a *funding* strategy whose whole thesis
is collecting funding, so its measured net **omits its main revenue line** — it's
judged on price PnL minus fees only. This systematically understates (or
mis-signs) FEMR's edge. *Fix:* attribute funding to the agent that holds the
position (the cloid→position map already exists in spirit). The backtest engine
already folds funding into realized PnL, so sim and live will disagree until this
is fixed live.

### C5 — Sharpe-based gates can never fire for real agents
`funding_arb_v1.yaml`'s primary goal and promotion both key on `sharpe`, but
`score_agent` only computes Sharpe/drawdown for `_account` (needs an equity
curve). For any real agent `sharpe is None` → N/A → the promotion gate can never
pass. The FEMR/TWAP configs were already migrated to `edge_bps`/`net_pnl`;
`funding_arb` was not. *Fix:* either compute per-agent equity curves (the
backtest engine now does this) or standardize all gates on per-agent metrics.

### C6 — liq_cascade is effectively dead
`cli/main.py::_enrich_view` posts `{"type":"liquidations"}` to `/info`, which is
not a real Hyperliquid info endpoint, so `liquidations` is ~always empty and the
agent never trades. Real liquidation data comes from the WS `trades` feed (each
trade carries a liquidation flag) or the explorer. *Fix:* source liquidations
properly or retire the agent until it can be fed.

### C7 — Signal horizon ≫ action cadence
MR / cascade edges decay in seconds–minutes, but the loop is a 5-min cron with a
**1-hour per-coin cooldown** and taker fills. By the time the bot acts, the
deviation has often reverted or extended. The cadence/edge mismatch alone can
flip a real edge negative. *Fix:* event-driven (WebSocket) execution for the
fast strategies, or restrict to genuinely low-frequency edges (carry/basis) that
tolerate a 5-min loop.

## Medium findings

- **M1 — Entry price recorded ≠ fill price.** In `femr_tick`, on a confirmed
  fill the code logs the decision with `d.px` (pre-trade mid), not
  `res.avg_px`. Stops/TPs are then computed off the intended price, not the real
  fill. Small at current size; wrong in principle.
- **M2 — `positions` table never populated.** Per-agent attribution is inferred
  from the decision log (binary owned/not-owned), so partial fills and size
  drift aren't tracked. The schema's `positions` table and the README's "replay
  engine" are unbuilt. The backtest engine is the natural home for a fills→position
  replay.
- **M3 — Two execution paths.** `agents/runtime.py::run_tick` (the "safe" path
  with `force_paper=True`) is **not** what live uses; `cli/main.py::femr_tick`
  has its own live loop. The safe wrapper is dead code for live. Consolidate.
- **M4 — Auto-tuner polishes a losing system.** `scripts/auto_tuner.py` asks an
  LLM to nudge params within ±20–50%. It cannot fix taker-vs-maker, cadence, or a
  structurally wrong strategy — the things that actually matter. Keep it, but it's
  a fine-tuner, not an edge source. *(Hardened, Iter 63: auto-apply is now
  risk-tightening only; loosening/scaling changes go to
  `agent_overrides.tuner_proposed.json` for human merge against backtest
  evidence. `HLBOT_TUNER_APPLY_LOOSENING=1` restores the old behavior.)*
- **M5 — basis spot scaling is fragile.** The `spotMetaAndAssetCtxs` price
  normalization (wei-decimals, "U"-prefixed wrapped tokens, 5% sanity band) is
  brittle and easy to get silently wrong; basis on BTC/ETH/SOL is also tiny and
  well-arbitraged, so net-of-taker edge is unlikely.
- **M6 — Hardcoded trader address** (`0x5C3a…`) in `exec/orders.py` and
  `scripts/daily_scorecard.py`. Fine for one deployment; move to config.

## DevOps findings

- **D1 — CI was red** *(fixed)*. `ci.yml` runs `ruff check .` but two `B007`
  errors lived in `scripts/daily_scorecard.py`; the `Makefile` only linted
  `src tests`, hiding the drift. Fixed the code and aligned `make lint` to
  include `scripts`.
- **D2 — No coverage of the live path or agents' `decide()`.** Tests cover risk/
  supervisor/health; the agents themselves were untested until the backtest tests.
- **D3 — Secrets/ops are out-of-repo** (Hermes config, systemd, EC2 sync via
  `scp`). Reasonable, but the deploy/runbook isn't documented in-repo.

## Strategy-by-strategy

- **FEMR** (funding mean-reversion): right *idea* (extreme funding mean-reverts),
  wrong *economics at $20 taker*. Round-trip cost > funding collected over the
  short hold. Belongs as a maker, hold-to-collect carry trade. Fix C4 first or
  you can't even measure it.
- **TWAP-MR**: classic trend-fade loser without a regime filter (C3). The veto
  layer is a band-aid; the regime filter is the cure.
- **liq_cascade**: dead (C6).
- **basis**: tiny edge, taker tax, fragile data (M5). Lowest priority.
- **funding_arb** (`funding_arb.py`): explicitly a reference skeleton, not wired
  to live.
- **veto**: useful as a guard; not an alpha source (by design).

## Bottom line

The repo is a **well-built chassis with no engine**. The chassis (safety,
accounting, supervision, risk-scaling) is worth keeping and is most of the hard
part. The missing engine is *a strategy with durable positive edge after realistic
costs* — and the missing tool to find one was the backtester, now built. The
fastest route to any positive edge is almost certainly **maker execution + a
regime-aware/carry strategy**, validated in the backtester before it touches
capital.
