# Progress log (append-only, newest at bottom)

Each iteration appends: date, what changed, why, evidence (test/lint/backtest
numbers), and what's next. Negative results are recorded too — they prune the
search.

---

## Iteration 0 — 2026-06-08 — review, keystone, and the loop

**Context.** First comprehensive review of the repo. Found a well-built safety/
accounting chassis with no profitable engine: every live agent bleeds after
costs, and there was no way to discover edge before risking capital.

**Changed.**
- Added the **backtest harness** `src/hl_bot/backtest/` (`engine.py`, `data.py`)
  and the `hlbot backtest` CLI command. It replays an agent's real `decide()`
  over historical frames with an explicit cost+funding model, writes synthetic
  fills, and scores via the production `score_agent`. Includes a `maker` flag to
  isolate the taker tax and equity-curve Sharpe/maxDD/Calmar. Tests in
  `tests/test_backtest.py`.
- Fixed **red CI**: ruff `B007` in `scripts/daily_scorecard.py`; aligned
  `make lint` to lint `scripts` too (it had drifted from CI's `ruff check .`).
- Wrote `docs/REVIEW.md` (full findings) and `docs/ROADMAP_TO_1M.md` (the
  numbers-first path), and stood up this Ralph loop (`ralph/`).

**Evidence.** `uv run pytest -q` → **47 passed** (5 new). `uv run ruff check .`
→ **All checks passed**. Backtest tests confirm: a mean-reversion path scores
profitable without costs; the same path nets strictly less as a taker; the
simulated clock drives max-hold exits.

**Findings recorded for follow-up.** REVIEW C1–C7, M1–M6, D1–D3 → mapped to
backlog B1–B17.

**What's next.** B1 — quantify the taker→maker gap on twap_mr/femr over real
history. **Blocked here:** the sandbox cannot reach api.hyperliquid.xyz (403), so
this must run where HL history is reachable, or implement B1a (offline cache)
first. If unblocked, the immediate hypothesis to test is: *most of the bleed is
the spread*, which would make B2 (maker execution) the highest-value next build.

---

## Iteration 1 — 2026-06-08 — production-hardening toward a safe go-live

**Context.** Directive: "start the loop, move it toward production and live." Did
NOT enable live trading — strategies still have negative edge after costs and the
switch is human-gated by design (see `docs/GO_LIVE.md`). Instead worked the P0/P1
items that *earn* the right to go live. B1 stays network-blocked here, so I took
the highest-value unblocked items.

**Changed (4 green-gated commits).**
- **B8 — real fill price.** `femr_tick` logged the pre-trade mid as the entry on a
  confirmed fill; now records `res.avg_px`/`res.filled_sz` so stops/TPs/age key
  off the real fill. (REVIEW M1.)
- **B3 — twap_mr_regime_v1.** New agent that consults `regime_allows_fade` and
  drops fades against a strong trend — the #1 loss loop (REVIEW C3). Plumbed a
  trailing `closes` series through `backtest.Frame` and the live `_enrich_view`.
  **Evidence:** cost-free synthetic uptrend, baseline TWAP nets **−$7.88** fading
  the rip; regime variant nets **−$4.00** (2 trades vs 4) by refusing those fades
  (`tests/test_regime_twap.py`). Real-data tuning pending B1.
- **B2 (primitive) — maker execution.** `place_limit_order` (post-only "Alo" so it
  can never become a taker), `round_price_to` (HL tick rules), `has_resting_order`.
  Tested via fake exchange + rounding cases. The *payoff* half (async resting-order
  fill reconciliation + routing live entries to maker) is queued as **B2b**.
- **B14 — go-live runbook.** `docs/GO_LIVE.md`: the gated checklist, secrets/env,
  promote/kill-switch/rollback, monitoring. The honest definition of "live."

**Evidence.** 47 → **56 tests pass**; `ruff check .` clean each commit.

**Readiness.** Still **NOT live-ready** (by design): no strategy has passed G0
(needs real-history backtests — B1), and live entries are still taker until B2b.
The regime TWAP is the leading G0 candidate.

**What's next.** B1a (offline history cache) to unblock B1 in this sandbox, then
B2b (async maker entries) — together they let the regime/carry strategies be
backtested *and* executed as makers, which is the whole ballgame for a positive
live edge.

---

## Iteration 2 — 2026-06-08 — all four go-live tracks, offline pieces

**Context.** Operator picked all four paths (prove-edge-first, tiny-live-pilot,
capital/AUM, run-loop-unattended). Each track's network/keys-gated final step is
the operator's; I built every offline piece for all four, green-gated.

**Changed (3 commits).**
- **B1a — offline history cache** (*prove-edge-first* + *unattended*).
  `save_frames`/`load_cached_frames`/`cached_or_fetch` (gzipped JSON under
  `data/backtest_cache/`, gitignored); `hlbot backtest-fetch`; `hlbot backtest
  --cache` runs without network. Makes backtests reproducible and the loop
  runnable unattended. Tested: cache round-trip + identical fresh-vs-cached score.
- **B15 — track-record export** (*capital/AUM*). `reports/track_record.py` +
  `hlbot track-record` → `track_record.{json,md}`: account equity curve +
  per-agent net/edge/Sharpe/$DD via the live `score_agent`. The artifact a vault
  depositor/allocator needs. Tested.
- **Pilot prep** (*tiny-live-pilot*). Wired `twap_mr_regime_v1` into the live
  roster (paper unless explicitly enabled), registered it for attribution/
  reporting, added `configs/twap_mr_regime_v1.yaml` with conservative gates.
  Operator flips one switch (docs/GO_LIVE.md). Nothing went live.
- **Unattended docs** — `ralph/README` "Unattended operation" (tmux/systemd,
  network/keys requirements, STOP file).

**Evidence.** 56 → **59 tests pass**; `ruff check .` clean each commit; all 4
goals configs validate; CLI imports clean with the 5-agent roster.

**Operator actions to advance each track (need your env/keys):**
1. *Prove edge first:* in an HL-reachable host, `uv run hlbot backtest-fetch
   --coins BTC,ETH,SOL,HYPE --days 120` then `uv run hlbot backtest --agent
   twap_mr_regime_v1 --compare`. Read the taker→maker gap + net edge. If positive
   → B2b, then promote via the gate.
2. *Tiny live pilot:* after a paper window, enable in agent_state per
   docs/GO_LIVE.md (`twap_mr_regime_v1` → live_small), watch the first ticks.
3. *Capital/AUM:* `uv run hlbot track-record` to produce the shareable record.
4. *Unattended:* run `ralph/loop.sh` under tmux/systemd on that host.

**What's next (loop).** B2b (async maker fill reconciliation + route live entries
to maker) — the payoff half of B2 — and B6/B7 (per-agent funding + Sharpe) so the
gates measure truth.

---

## Iteration 3 — 2026-06-08 — new strategies + the confirmation gate

**Context.** Operator approved the full strategy slate. Built the confirmation
machinery first (so "confirm a strategy" is one rigorous command), then the two
highest-conviction NEW strategies and confirmed their mechanics on synthetic
funding scenarios. Real-history confirmation is one command on a net host (B1).

**Changed (2 commits).**
- **B5 — confirmation harness (G0 as code).** `backtest/confirm.py`: walk-forward
  (in-sample vs held-out OOS) + cost stress (maker, taker 1×/2×/3× slippage) →
  explicit PASS/FAIL with 2×-slippage robustness. `hlbot confirm`. Tests prove it
  confirms a real range-bound MR edge and rejects a trend-fader.
- **B4 — two maker/carry strategies (slate #1, #2).**
  - `xfund_carry_v1`: market-neutral cross-sectional funding carry (short top-K
    highest-funding, long bottom-K most-negative; dollar-neutral). Highest
    conviction; best fit for capital/AUM.
  - `funding_carry_v1`: single-name maker carry (hold to collect, no TP churn,
    wide stop) — fixed-economics replacement for taker FEMR.
  - Engine `liquidate_at_end` so held-carry funding lands in the realized
    scorecard. Both registered in backtest/confirm factories + KNOWN_AGENTS +
    gated goals configs. Confirm-only until they pass G0 on real data.

**Evidence.** 62 → **65 tests pass**; lint clean; all 6 goals configs validate.
Synthetic: single-name carry collects funding from an extreme coin; x-sectional
is two-sided and skips calm-funding coins; harness confirms MR-maker and rejects
trend-taker.

**Slate status.** #1 xfund_carry ✅ built+confirmed(synthetic). #2 funding_carry
✅ built+confirmed(synthetic). #3 regime-maker MR — the *strategy* is built and
confirmable via `confirm --prefer maker`; the live maker *execution* plumbing is
B2b (next, scoped carefully — it touches the live order path so I won't rush it).
#4 basis — already an agent and confirmable via the harness; low conviction.

**Operator action (advances all four):** on an HL-reachable host —
`uv run hlbot backtest-fetch --coins BTC,ETH,SOL,HYPE,AVAX,LINK --days 120` then
`uv run hlbot confirm --agent xfund_carry_v1 --prefer maker` (repeat per agent).
Confirmed agents earn the paper roster, then the live gate in docs/GO_LIVE.md.

**What's next (loop).** B2b (async maker live execution) and B6/B7 (honest
per-agent funding + Sharpe so the gates measure truth).

---

## Iteration 4 — 2026-06-08 — maker live execution + infra

**Context.** "Keep going, get it to production" + an infra question. Built the
remaining real execution piece (B2b) and the deployment/infra guide.

**Changed (2 commits).**
- **B2b — maker live execution.** Maker orders rest across ticks, so added the
  lifecycle state machine `exec/maker.py` (rest → fill-detect → place /
  cancel-stale), `cancel_order`, the `rest` decision action, and
  `femr_tick --execution maker` (default stays taker; exits stay taker for
  urgency). Ownership still keys off 'place', so a resting quote isn't a position
  until it fills. Logic fully unit-tested offline (in-memory DB); the live
  exchange calls can't be validated in CI, so first live use must be watched at
  tiny size. Next: book-aware limit pricing (needs WS L2, B10/B-book).
- **docs/INFRA.md** — 24/7 deployment (systemd, Litestream, monitoring), infra
  tiers + costs, and what to invest in for signal (WebSocket = highest-value free
  upgrade; fixes liq_cascade) and execution (maker first; latency/colo only once a
  strategy needs it). Honest headline: at current cadence, edge+capital are the
  bottleneck, not latency.

**Evidence.** 65 → **69 tests pass**; lint clean.

**Production status.** The *code path* to live maker execution now exists and is
gated. Still NOT live: needs (1) a strategy confirmed on real history (B1/B4-RUN),
(2) one watched live-small run per docs/GO_LIVE.md, (3) Tier-0 infra from
docs/INFRA.md. The build side of all four slate strategies is done; the gating
steps are operator/network actions.

**What's next (loop).** B10 (WebSocket market view — sub-second mids, L2 depth,
real liquidations), B6/B7 (per-agent funding + Sharpe), B-book (book-aware maker
pricing), B14a (codify systemd/Litestream deploy in-repo).

---

## Iteration 5 — 2026-06-08 — automate everything + deployable

**Context.** "Automate everything; get it to deployment." Built the ops layer, the
one-command deploy, and the WebSocket signal upgrade.

**Changed (3 commits).**
- **Ops automation.** `hlbot health` (ok/warn/down from tick/ingest freshness,
  equity, paused agents, 24h PnL) pings a HEALTHCHECK_URL dead-man switch +
  Telegram-alerts; `hlbot doctor` preflight (env, DB, configs, API-wallet perms,
  HL reachability) gates deploy/go-live. (`ops/health.py`, `ops/doctor.py`, tests.)
- **Deployment automation (B14a).** `deploy/install.sh` — idempotent one-command
  install on a fresh Ubuntu host (uv, locked-down hlbot user, systemd timers,
  /etc/hl-bot/env 600, optional Litestream S3 backups, doctor preflight). Units:
  hlbot-tick (5m: ingest→agents→supervisor→health), hlbot-report (daily),
  hlbot-loop (Ralph loop, manual-enable). PAPER by default; live = HLBOT_TICK_ARGS
  + agent_state per GO_LIVE. `deploy/README.md`, Makefile check/deploy.
- **B10 — WebSocket market view.** `ingest/ws.py` MarketState + `hlbot ws` service
  → snapshot; live tick overlays it for sub-second mids, L2 book_top, and a real
  liquidations feed (fixes the dead liq_cascade, C6), REST fallback. State machine
  unit-tested with synthetic frames.

**Evidence.** 69 → **82 tests pass**; lint clean; CLI imports; shell scripts
`bash -n` clean.

**Deployability.** The repo is now **turnkey-deployable to run 24/7 in paper**:
`bash deploy/install.sh` → systemd timers + monitoring + backups + WS feed. Going
live remains the gated sequence in docs/GO_LIVE.md (confirm a strategy on real
history → API wallet → enable agent → set live args). The autonomous loop can run
as a service to keep improving the repo.

**What's next (loop).** B6/B7 (per-agent funding attribution + Sharpe so gates
measure truth), B-book (book-aware maker pricing using WS L2), userFills WS for
instant maker-fill detection, and B4-RUN (confirm carry strategies on real data).

---

## Iteration 6 — 2026-06-08 — env-config, managed WS, AWS deploy

**Context.** "Deploy on AWS, decide everything, get it running." Built the AWS
one-`apply` path and removed the last hardcoded-account blocker.

**Changed (2 commits).**
- **B13 — HL_TRADER_ADDRESS via env** (`_resolve_trader_address`: HL_TRADER_ADDRESS
  → HL_ADDRESS → legacy default) in orders.py + daily_scorecard; tested. No more
  hardcoded account.
- **Managed WS service** — `deploy/systemd/hlbot-ws.service` (Restart=always),
  auto-enabled by install.sh; HLBOT_WS_SNAPSHOT/COINS in env so the tick overlays
  it by default.
- **docs/HOST_QUICKSTART.md** — full nothing→24/7 runbook incl. real-data confirm
  gate + gated go-live.
- **AWS automation (`deploy/aws/`)** — Terraform: latest Ubuntu 24.04 arm64,
  t4g.small in Tokyo (ap-northeast-1), encrypted gp3, security group, **IAM
  instance role** for Litestream S3 backups (no static keys), and cloud-init
  user-data that runs install.sh on boot → instance comes up running PAPER.
  install.sh now renders litestream.yml to concrete values + adds an
  EnvironmentFile drop-in; env file is 640 root:hlbot so the bot user can read it.

**Evidence.** 85 tests pass; lint clean; all shell scripts `bash -n` clean.
Terraform/HCL written carefully but NOT validated in CI (no terraform/creds in the
sandbox) — run `terraform validate`/`plan` before `apply`.

**Cannot do from the sandbox:** provision AWS (no creds/network) or trade live (no
keys, HL firewalled). Deployment is now one `terraform apply` on the operator side;
going live stays the gated confirm→enable sequence.

**What's next (loop).** B6/B7 (per-agent funding + Sharpe), B-book (book-aware
maker pricing), userFills WS, B4-RUN (confirm on real history).

---

## Iteration 7 — 2026-06-08 — honest metrics + set-and-forget autonomy

**Changed (2 commits).**
- **B6/B7 — per-agent funding attribution + Sharpe.** `_coin_holders_over_time`
  replays per-agent ownership from the decision log; `_agent_funding_payments`
  splits each funding payment among concurrent holders (sums to total, no
  double-count); score_agent folds the share into net/edge and computes per-agent
  daily-PnL Sharpe — so carry strategies are measured correctly and sharpe-gates
  can fire. Tested (5 cases).
- **Set-and-forget autonomy + novice AWS guide.** `deploy/update.sh` +
  `hlbot-update.timer`: the box pulls branch improvements (e.g. from the Ralph
  loop), TEST-GATES them, and restarts only if green — so a self-improving bot
  ships its own code without ever deploying red. Gated by HLBOT_AUTO_UPDATE=1
  (enabled by install.sh + AWS user-data). `docs/AWS_NOVICE_SETUP.md`: step-by-step
  console deploy → enable loop → gated go-live → off switch, written for a novice.

**Evidence.** **90 tests pass**; lint clean; all shell scripts `bash -n` clean.

**Autonomy model.** Infra is genuinely set-and-forget: 24/7 trading (paper by
default), self-improvement loop (writes green code), auto-deploy (test-gated), self-
governing risk (guardrails auto-pause/demote, capital floor halts), Telegram alerts.
The two irreducible human steps for LIVE money: provide the API/agent wallet once,
and flip HLBOT_TICK_ARGS to --live for a confirmed agent. Losses bounded by
guardrails; the loop is forbidden from enabling live or raising caps.

**What's next (loop).** B-book (book-aware maker pricing via WS L2), userFills WS,
B4-RUN (confirm carry on real history), B16 (HL vault eval for AUM).

---

## Iteration 8 — 2026-06-08 — book-aware maker pricing + fastest live path

- **B-book** — `maker_limit_price` joins the near touch (best bid/ask) from the WS
  L2 book; live maker entries price off `view.book_top` (fallback mid, never
  cross). Tested. 91 tests pass.
- **Fastest live path** — `docs/AWS_NOVICE_SETUP.md` FAST PATH: one user-data block
  brings the box up live-armed (agent enabled, maker exec, loop on); trades start
  the moment the API wallet is added (one post-boot command). Off switch one line.

---

## Iteration 9 — 2026-06-08 — fills→positions replay (size-aware attribution)

**Context.** P0 is done or network-blocked; P1 honest-measurement was the top
unblocked lane. Picked **B9** (REVIEW M2): the `positions` table existed in the
schema but nothing wrote it, so per-agent attribution (the funding split in
`scoring.metrics`) inferred ownership from the decision log as a *binary*
owned/not-owned flag — blind to partial fills, size drift, and manual
interference.

**Changed (1 commit).**
- **B9 — fills→positions replay.** New `src/hl_bot/db/positions.py`:
  - `replay_positions(fills)` — pure, DB-free fold of time-ordered fills into
    per-(agent, coin) `PositionRow`s: net size (B=+, A=−), size-weighted average
    entry (recomputed only when the position grows in-direction; untouched on
    reduction; re-based to fill px on a flip through zero), realized PnL taken
    straight from the exchange `closed_pnl` (never recomputed — keeps the
    "ground truth from the exchange" invariant), and summed fees.
  - `rebuild_positions(conn)` — full DELETE + re-insert from `fills` ordered by
    time; idempotent, so re-running after new fills land yields correct current
    state with no drift/dupes.
  - Wired into `ingest_fills`: rebuilds only when new fills were inserted.

**Evidence.** `uv run pytest -q` → **98 passed** (7 new in `tests/test_positions.py`:
weighted entry on partials, reduction keeps entry + exchange PnL, full close
resets entry, flip-through-zero re-bases entry, fees/keys across agents+coins,
idempotent table write, NULL-agent fills skipped). `ruff check src tests scripts`
→ clean.

**Why it matters.** This is the size-aware substrate the binary decision-log
attribution lacked (REVIEW M2). It makes per-agent ownership robust to partial
fills and manual trades, and is the natural home for upgrading the funding split
from equal-among-holders to size-weighted later.

**What's next (loop).** Consider upgrading `scoring.metrics` funding attribution
to weight by `positions.net_sz` (size-aware split) now that the substrate exists;
B11 (feed/retire liq_cascade via WS liq data), B12 (consolidate execution paths),
B4-RUN (confirm carry on real history — still network-blocked).

---

## Iteration 10 — 2026-06-08 — size-weighted funding attribution

**Context.** P0 is done or network-blocked; P1 honest-measurement remains the top
unblocked lane. Iteration 9 built the size-aware fills→positions substrate (B9)
and flagged the natural next step: the funding split in `scoring.metrics` was
still **equal-among-holders** (each concurrent holder of a coin got 1/N of the
funding regardless of how much they held). For a cross-sectional carry book where
two agents hold very different sizes of the same coin, that mis-attributes the
revenue line the whole strategy is judged on (REVIEW C4).

**Changed (1 commit).**
- **B6-size — size-weighted funding split.** `scoring/metrics.py`:
  - `_coin_agent_sizes_over_time(conn)` — replays the `fills` stream (B=+, A=−)
    into a per-coin chronological timeline of each agent's |net size|. Fills are
    the ground truth for size, so this is the same invariant as B9, used here for
    a time-resolved query rather than a current snapshot.
  - `_sizes_at(events, t)` — each agent's |net size| as of the last fill at-or-
    before the funding instant `t`.
  - `_agent_funding_payments` now splits each payment in proportion to holder
    |net size| when fills-derived positions exist, and falls back to the prior
    equal-among-decision-log-holders split when they don't (e.g. positions opened
    before fills were ingested, or manual). Shares still sum to the total with no
    double-count; a fully-closed holder at `t` correctly collects nothing.

**Evidence.** `uv run pytest -q` → **100 passed** (+2 new in test_attribution.py:
3:1 size-weighted split, and a holder who closed before funding gets 0 while the
remaining holder gets the whole payment). `ruff check src tests scripts` → clean.
Existing decision-log attribution tests (no fills) still pass via the fallback.

**Why it matters.** The carry strategies (xfund_carry_v1 cross-sectional,
funding_carry_v1 single-name) are scored on funding, and the promotion/confirm
gates key off that net. Equal-split flattered/penalized agents by holder *count*,
not capital at risk; size-weighting makes the measured edge match the economics.
This is measurement-only — no strategy, risk, or live change.

**What's next (loop).** B11 (feed/retire liq_cascade via WS liq data), B12
(consolidate the two execution paths so the safe wrapper is what live uses), B4-RUN
(confirm carry on real history — still network-blocked at api.hyperliquid.xyz).

---

## Iteration 11 — 2026-06-08 — retire the dead liquidations endpoint (B11)

**Context.** P0 is done or network-blocked. Top unblocked item was **B11**
(REVIEW C6): liq_cascade was "dead" because `_enrich_view` posted
`{"type":"liquidations"}` to `/info` — not a real Hyperliquid info endpoint, so it
always returned nothing. B10 (Iteration 5) already added the *correct* source (the
WS `trades` liquidation flag), but the misleading dead REST call still sat in the
live path, making it look like REST fed the agent when it never could.

**Changed (1 commit).**
- **B11 — feed liq_cascade only from WS; remove the fake endpoint.**
  `cli/main.py::_enrich_view` no longer makes the `{"type":"liquidations"}` REST
  call; `view.extra["liquidations"]` defaults to `[]`. The WS snapshot overlay
  (gated on `HLBOT_WS_SNAPSHOT`, 30s freshness) is now the *sole* source — so when
  no WS feed is present the agent simply holds (honestly disabled, never blind to
  garbage). Added a tick-time warning when `liq_cascade_v1` is in the roster but
  `HLBOT_WS_SNAPSHOT` is unset, so an operator isn't left wondering why it never
  trades. Docstring updated to state the no-public-endpoint fact.
- **tests/test_liq_cascade.py** (new, 4 cases) — the agent's `decide()` had no
  direct coverage. Pins the fed-or-inert contract: WS-shaped >$100k short-liq on a
  high-volume coin → enter LONG (same side as cascade); empty feed → `hold`;
  thin-coin below the volume floor → no entry; stale event outside the 5m window →
  no entry.

**Evidence.** `uv run pytest -q` → **104 passed** (+4). `ruff check src tests
scripts` → clean. CLI import smoke-tested OK.

**Why it matters.** Removes the last "looks-wired-but-dead" path flagged in the
review (C6). liq_cascade is now either genuinely fed (WS) or transparently
disabled — no false signal, no silent inertia. No strategy/risk/live change.

**What's next (loop).** B12 (consolidate `runtime.run_tick` vs `femr_tick` so the
safe wrapper is what live uses — REVIEW M3), B7 (standardize remaining goal configs
on per-agent metrics now that per-agent Sharpe exists — C5), B4-RUN (confirm carry
on real history — still network-blocked at api.hyperliquid.xyz).

---

## Iteration 12 — 2026-06-08 — kill the last dead supervisor gate (B7/C5)

**Context.** P0 is done or network-blocked; P1 honest-measurement is the top
unblocked lane. Iteration 7 made per-agent Sharpe compute, but B7/C5 had a tail:
`scoring.metrics.score_agent` still returns `max_drawdown`/`calmar` = `None` for
every real agent (they need a capital base; only the synthetic `_account` has an
equity curve). `funding_arb_v1.yaml` was the **one** config gating on
`max_drawdown` — a 7d **demote** guardrail at −10% that could therefore *never
fire*. Same "dead gate" defect as C5, but on a risk control rather than promotion.

**Changed (1 commit).**
- **`configs/funding_arb_v1.yaml`** — replaced the un-fireable `max_drawdown`
  demote guardrail with an `edge_bps` (7d, ≥ −10 bps) demote, consistent with the
  carry configs (`xfund_carry_v1`, `funding_carry_v1`). Tightening-only and now
  actually computable for a real agent. Graduated response preserved: alert at
  −5 bps, demote at −10 bps, pause on 24h net ≤ −$200.
- **`supervisor/goals.py` docstring** — the canonical example used the same dead
  `max_drawdown` guardrail; swapped it for `edge_bps` and added a note that
  `max_drawdown`/`calmar` are `_account`-only, so real-agent gates must key on
  net_pnl / edge_bps / sharpe / win_rate / n_trades.
- **`tests/test_supervisor_configs.py`** (+2) — `test_no_config_gates_a_real_
  agent_on_account_only_metrics` loads every config and asserts no real-agent goal/
  guardrail/promotion/demotion references `max_drawdown`/`calmar` (codifies C5 as a
  cross-config regression guard); `test_funding_arb_demote_fires_on_negative_edge`
  proves the previously-dead guardrail now demotes a bleeding live_small agent to
  paper.

**Evidence.** `uv run pytest -q` → **106 passed** (+2). `ruff check src tests
scripts` → clean. Measurement/governance-only — no strategy, sizing, or live change.

**Deliberately NOT done.** Computing a real per-agent fractional maxDD/calmar
needs a capital-base convention (the backtester anchors on `starting_capital`;
live per-agent scoring has no base). Inventing one carelessly would feed a
misleading number to a live demote gate, so it's parked as **B7-dd** pending a
design decision, rather than rushed here.

**What's next (loop).** B12 (consolidate `runtime.run_tick` vs `femr_tick` so the
safe wrapper is what live uses — REVIEW M3), B7-dd (capital-base convention for
per-agent drawdown), B4-RUN (confirm carry on real history — network-blocked).

---

## Iteration 13 — 2026-06-08 — consolidate the two execution paths (B12/M3)

**Context.** P0 is done or network-blocked. The top unblocked item was **B12**
(REVIEW M3): `agents/runtime.run_tick` (the "safe" path) and
`cli/main.femr_tick` (the live loop) each had their own agent-decision loop. The
safe wrapper was effectively dead for live, and — more importantly — the live
loop's copy had **no exception handling**: a single agent raising in `decide()`
would crash the whole live tick (no decisions logged, no error recorded, the
remaining agents never consulted).

**Changed (1 commit).**
- **`agents/runtime.py` — new `collect_decisions(conn, agents, view, *,
  is_paper, defer_actions)`**: the single decision-gathering core. Wraps each
  `decide()` so a raising agent logs an `error` decision and the loop continues;
  tags every decision with `is_paper`; logs immediately *except* actions in
  `defer_actions` (returned for the caller to act on/display but not yet logged).
  `run_tick` now filters enabled agents then delegates to it.
- **`cli/main.py::femr_tick`** — replaced its bespoke decide-loop with
  `collect_decisions(..., is_paper=not live, defer_actions={"hold","place",
  "flatten"})`. Same defer semantics as before (place/flatten logged only after
  exchange acceptance so cooldown checks don't see our own intent; hold shown but
  not logged) — but now the live path gets the safe wrapper's per-agent error
  isolation it was missing.
- **`tests/test_collect_decisions.py`** (new, 3 cases) — raising agent is
  isolated (logged as `error`, sibling agent still runs, error not returned);
  deferred actions collected-but-not-logged while non-deferred are logged; and
  `is_paper` applied uniformly.

**Evidence.** `uv run pytest -q` → **109 passed** (+3). `ruff check src tests
scripts` → clean. `python -c "import hl_bot.cli.main"` → ok.

**Why it matters.** Removes the duplicate loop the review flagged (M3) so the two
paths can no longer drift, and closes a real robustness gap: the live loop now
survives one agent throwing instead of taking the whole tick down. Behavior of
the live execution/risk machinery is otherwise unchanged — measurement/robustness
only, no strategy, sizing, or live-mode change.

**Deliberately NOT done.** Did not fold femr_tick's risk/reconcile/execution
stages into runtime — those are genuinely live-only and out of scope for a single
reviewable slice; the duplication that mattered (the decide-loop) is gone.

**What's next (loop).** B7-dd (capital-base convention for a real per-agent
maxDD/calmar — needs a design decision), B-book (book-aware maker pricing off L2
depth, B10 done), B4-RUN / B1 (confirm carry on real history — still
network-blocked at api.hyperliquid.xyz).

---

## Iteration 14 — 2026-06-08 — per-agent dollar drawdown (B7-dd / C5)

**Context.** P0 is done or network-blocked; P1 honest-measurement is the top
unblocked lane. B7-dd was the explicitly-parked tail of B7/C5: for a real agent
`score_agent` returns `max_drawdown`/`calmar` = `None` (the *fractional* DD needs
a capital base; only the synthetic `_account` has an equity curve). Iteration 12
removed the one config that gated on it, but there was still **no computable
per-agent drawdown** for a risk gate to key on. The parked worry was that
inventing a fractional per-agent DD would feed a misleading number to a live
demote gate.

**Design decision.** The honest resolution is to *not* invent a fractional DD at
all: add a **dollar** drawdown. A fractional DD needs a capital base; a *dollar*
peak-to-trough give-back of the cumulative net-PnL curve does **not** — so it's
computable for every real agent and meaningful for a tightening-only per-agent
gate (e.g. demote if 7d give-back exceeds $X). Notably the dollar-DD logic already
existed in `reports/track_record.py` but wasn't on the `Scorecard`, so the
supervisor couldn't see it. This lifts it to the single source of truth.

**Changed (1 commit).**
- **`scoring/metrics.py`** — new `Scorecard.max_drawdown_usd` (`float | None`) +
  `_dollar_max_drawdown(daily)` (cum-PnL curve seeded at a flat $0 baseline, so an
  agent that only loses has a drawdown equal to its total loss; result ≤ 0).
  Computed for real agents from the same daily-PnL series used for Sharpe
  (trades net of fees **plus** attributed funding, chronological order — gap days
  contribute 0). For `_account` it's the dollar peak-to-trough of the equity
  curve. The fractional `max_drawdown` stays `_account`-only and unchanged.
- **`reports/track_record.py`** — dropped its private `_dollar_max_drawdown`;
  per-agent `max_drawdown_usd` now comes from `score_agent(...).max_drawdown_usd`
  (now also folds funding into the curve — more correct, and the exact number the
  supervisor would gate on).
- **`supervisor/goals.py`** docstring — documents `max_drawdown_usd` as the
  capital-base-free per-agent drawdown gates may use.
- **`tests/test_attribution.py`** (+4) — per-agent dollar DD on a
  +100/−40/+5 curve = −$40 (and fractional `max_drawdown` stays N/A); a
  winners-only agent = $0; no-activity = N/A; account dollar DD from a
  1000→1200→900→1000 equity curve = −$300.

**Evidence.** `uv run pytest -q` → **113 passed** (+4). `ruff check src tests
scripts` → clean. Measurement/governance-only — no strategy, sizing, or live
change.

**Also.** Ticked stale backlog boxes that code already satisfied but were never
checked: **B-book** (book-aware maker pricing, done Iteration 8) and **B13**
(env-config trader address, done Iteration 6).

**What's next (loop).** A per-agent `max_drawdown_usd` *demote* guardrail on the
live-candidate configs now that the metric is computable (tightening-only); a
dollar-calmar (net / |dollar DD|) if a ratio gate is wanted; B4-RUN / B1 (confirm
carry on real history — still network-blocked at api.hyperliquid.xyz).

---

## Iteration 15 — 2026-06-08 — wire the dollar-drawdown demote guardrail (B7-dd-gate / C5)

**Context.** P0 is done or network-blocked (api.hyperliquid.xyz 403 in sandbox).
The top unblocked item was the explicit Iteration-14 "what's next": now that
`Scorecard.max_drawdown_usd` is computable for every real agent (dollar
peak-to-trough give-back, needs no capital base), put it to work as a
**tightening-only** per-agent risk gate. Iteration 14 added the metric; nothing
gated on it yet.

**Changed (1 commit).**
- **All six real-agent configs** (`funding_arb_v1`, `twap_mr_v1`,
  `twap_mr_regime_v1`, `xfund_carry_v1`, `funding_carry_v1`, `femr_v1`) — added a
  `{metric: max_drawdown_usd, window: 7d, op: ">=", threshold: -X, action: demote}`
  guardrail, X scaled to each agent's 24h pause limit ($400/$75/$75/$75/$50/$25
  respectively). `max_drawdown_usd` is ≤ 0, so `>= -X` passes while the week's
  give-back stays within $X and **demotes** (live→live_small→paper) when it
  exceeds it. Demote is tightening-only, consistent with the hard rule.
- **`tests/test_supervisor_configs.py`** (+1) —
  `test_dollar_drawdown_demote_fires_on_giveback`: an agent peaks at +$129 then
  bleeds to +$40 net. 7d net AND edge stay positive (edge_bps demote silent) and
  the 24h window is flat (no pause), yet the $89 give-back trips the $75 demote.
  Asserts the demote fired *from the give-back guardrail* (not edge) and the agent
  dropped live_small→paper — proving the new gate's distinct value.

**Why it matters.** edge_bps catches systematic bleed and net_pnl(24h) catches an
acute daily loss, but neither catches an agent that runs up gains then bleeds them
back while staying net-positive on the window — a classic "round-trip a paper
profit into a real loss" failure. The dollar-DD gate is the missing risk control
for exactly that, and it's the first live use of the metric added in Iteration 14.

**Evidence.** `uv run pytest -q` → **114 passed** (+1). `ruff check src tests
scripts` → clean. Governance/measurement only — no strategy, sizing, or live-mode
change; the new gate only ever *reduces* exposure.

**The C5-regression guard still holds.** `ACCOUNT_ONLY_METRICS = {max_drawdown,
calmar}` is an exact-set intersection, so `max_drawdown_usd` (a distinct string,
and genuinely per-agent computable) does not trip
`test_no_config_gates_a_real_agent_on_account_only_metrics`.

**What's next (loop).** A dollar-calmar (net / |dollar DD|) ratio gate if wanted;
B4-RUN / B1 (confirm carry on real history — still network-blocked at
api.hyperliquid.xyz); B14a deploy automation.

---

## Iteration 16 — 2026-06-08 — B1 UNBLOCKED: the taker tax, quantified on real history

**Context.** For 15 iterations B1 — the #1 backlog item and the central question of
the whole review (REVIEW C1) — was blocked because the sandbox couldn't reach
`api.hyperliquid.xyz` (403). **This iteration the network was open** (POST /info →
200), so I finally ran the real-data measurement the entire roadmap is gated on.

**What I did.**
- `hlbot backtest-fetch --coins BTC,ETH,SOL,HYPE,AVAX,LINK --interval 1h --days 120`
  → cached **2881 frames** under `data/backtest_cache/` (gitignored). Reproducible
  offline from here.
- `hlbot backtest --agent <a> --compare` for all seven agents.
- `hlbot confirm --prefer maker` (walk-forward + cost ladder) for both TWAP variants.
- A direct experiment: forced `xfund_carry_v1` to trade by lowering its funding
  threshold, to test whether carry survives costs *when it fires*.

**Evidence — the taker tax is real and ~73% of the bleed (C1 CONFIRMED).**
| agent | taker net / edge | maker net / edge | gap |
|---|---|---|---|
| twap_mr_v1 | −$192.73 / −7.7bps | −$52.09 / −2.0bps | ~5.7bps |
| twap_mr_regime_v1 | −$197.14 / −8.0bps | −$58.49 / −2.3bps | ~5.7bps |
Maker execution removes ~73% of the loss — the review's "most of the bleed is the
spread" hypothesis is now a measured fact, not a guess.

**Evidence — but maker alone does NOT create edge (G0 not passed by anything).**
`confirm --prefer maker` → **both TWAP variants NOT CONFIRMED**. As a maker they're
~flat *in-sample* (−0.3 to −0.4 bps) but clearly negative *out-of-sample* (−5.5 to
−6.2 bps, sharpe ≈ −2.4 to −2.8) — i.e. no edge that generalizes. The regime filter
(B3) that helped on a *synthetic* trend is **slightly worse than baseline on real
majors** (−2.3 vs −2.0 bps maker) — on this 120d window it didn't earn its keep.

**Evidence — carry/femr are DORMANT on liquid majors (the threshold mismatch).**
femr/funding_carry/xfund_carry/basis all produced **0 trades**. Root cause is *not*
a bug: realized hourly funding on BTC/ETH/SOL/HYPE/AVAX/LINK over 120d peaked at
**|f| ≈ 0.000065/hr (SOL ≈ 57% APR)**, while the entry thresholds demand far more
(xfund 0.0001/hr ≈ 88% APR; femr 0.00015/hr ≈ 130% APR). Liquid majors simply never
fund that hard, so these agents correctly hold.

**Evidence — and when forced to trade, carry still loses after costs.** Lowering
`xfund_carry` `enter_funding_per_hr` to make it fire:
| thr (/hr) | maker net / edge / trades | taker net / edge |
|---|---|---|
| 0.00005 | −$0.08 / −5.4bps / 6 | −$0.16 / −10.9bps |
| 0.00002 | −$1.34 / −5.1bps / 104 | −$2.76 / −10.6bps |
| 0.00001 | −$4.47 / −4.1bps / 440 | −$10.49 / −9.6bps |
Net-negative even as a maker at every threshold: the carry collected on majors is
smaller than maker cost + the residual directional noise of the (imperfectly
dollar-neutral) legs. So *lowering the threshold to chase carry on majors is not an
edge* — consistent with the no-raising-caps/tightening-only discipline.

**Honest conclusion (prunes the search).** Over the last 120d on the six most
liquid HL coins: (1) the taker tax is the dominant, now-quantified cost; (2) maker
execution is necessary but *not sufficient*; (3) neither TWAP-MR, its regime
variant, nor cross-sectional funding carry shows positive net-of-cost edge that
survives walk-forward. **No strategy passes G0 on this dataset.** The carry thesis
isn't dead — but it lives in *high-funding alts*, not majors. That's the next test.

**Evidence (gate).** `uv run pytest -q` → **114 passed**; `ruff check src tests
scripts` → clean. Measurement-only — no strategy/sizing/live change. The cache is
gitignored (data/), so this commit is docs + backlog only.

**What's next (loop).** **B1-alt** (the highest-leverage follow-up): fetch a
high-funding alt basket (where realized |funding| actually reaches the carry
thresholds) and re-run `confirm --agent xfund_carry_v1 --prefer maker` — this is the
honest test of whether funding carry has *any* edge. Then **B-femr-regime** (femr is
dormant on majors; either widen its universe or retire it). The TWAP family looks
like a dead end after costs even as a maker.
