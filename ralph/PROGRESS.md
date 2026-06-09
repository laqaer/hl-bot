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

---

## Iteration 17 — 2026-06-08 — B1-alt: carry has no edge on high-funding alts either (and a funding-data bug fixed)

**Context.** Iteration 16 (B1) proved no agent passes G0 on liquid majors and left
one open edge lead: **B1-alt** — the carry thesis can only be fairly tested where
realized |funding| actually reaches the agents' thresholds, i.e. high-funding alts,
not majors. Network is still open (POST /info → 200). This iteration ran that test
honestly — and first had to fix a measurement bug that would have silently faked it.

**Code change (the committed increment, with tests).**
- **`backtest/data.py`: paginate `fetch_funding_history`.** HL's `fundingHistory`
  returns at most **500 rows (~20.8d at 1h)**. A single call therefore silently
  truncated any longer window: every frame older than the last ~500h read
  `funding=0` (via `funding_rate_at`: most-recent row ≤ ts, none found → 0.0).
  For a *carry* backtest, where funding is the entire signal, that's fatal — the
  Iteration-16 majors carry numbers only had real funding for their last ~21d.
  Extracted `_fetch_funding_page` (one POST) + a pure `_paginate_funding` loop that
  advances the cursor past each page's last `time`, dedupes by `time`, and stops on
  a short/empty page or `max_pages`. `fetch_funding_history` now returns the full
  window. **Verified live:** the alt basket below now shows `funding` nonzero on
  **100%** of 2881 frames (was ~17%).
- **`tests/test_backtest.py` (+2):** `test_paginate_funding_covers_full_window`
  (1008 synthetic hourly rows reassembled across ≥3 capped pages, unique & ordered)
  and `test_paginate_funding_stops_on_short_page` (a <500-row first page is the last
  page — no wasted call). Pure, no network.

**Experiment — B1-alt (measurement; cache gitignored).**
- `backtest-fetch --coins INJ,PURR,TRUMP,AERO,NIL,APT,SPX,PYTH,EIGEN,S --days 120`
  → 2881 frames, full-window funding. Picked by scanning `metaAndAssetCtxs` +
  `fundingHistory` for liquid, persistently-high-|funding| perps. Realized 120d
  mean|f|: TRUMP 48%, PURR 36%, INJ 31%, AERO/NIL ~24–28%, others 14–23% APR.
- `confirm_strategy(prefer="maker")` with `min_daily_volume_usd=0` (so the
  high-funding, lower-liquidity alts aren't gated out — the point is to test the
  *funding* edge, not liquidity).

**Evidence — carry is NOT CONFIRMED on high-funding alts (G0 FAIL).**
| agent | in-sample (maker) | oos (maker) | maker full | taker-2x |
|---|---|---|---|---|
| xfund_carry_v1 | −3.6bps / sh −1.10 | **−16.8bps / sh −2.95** | −7.7bps (876 tr) | −15.2bps |
| funding_carry_v1 | −6.3bps / sh −0.91 | **−33.2bps / sh −3.58** | −16.5bps (416 tr) | −24.0bps |
Both agents now *trade plenty* (the threshold mismatch is gone), and both lose —
negative edge in- AND out-of-sample, across the entire cost ladder, even as makers.

**Evidence — selectivity doesn't rescue it.** xfund_carry maker, raising the entry
threshold to demand ever-more-extreme funding:
| enter/hr | ≈APR | net$ | edge_bps | trades | sharpe |
|---|---|---|---|---|---|
| 0.00003 | 26% | −31.06 | −4.5 | 2788 | −2.31 |
| 0.00010 | 88% | −16.81 | −7.7 | 876 | −1.82 |
| 0.00015 | 131% | −11.73 | −10.8 | 436 | −1.52 |
| 0.00020 | 175% | −15.58 | −19.0 | 328 | −2.26 |
Net-negative at *every* threshold, and per-trade `edge_bps` gets **worse** as you
pick the most-extreme funding — the highest-funding names carry more directional
risk than carry reward, even in a dollar-neutral book. Being pickier shrinks the
dollar loss only by trading less.

**Honest conclusion (prunes the search further).** Funding carry has **no
demonstrable net-of-cost edge on majors (B1) OR on high-funding alts (B1-alt)**.
The carry collected is structurally smaller than maker cost + the residual
directional variance of imperfectly-neutral legs, and concentrating into the
highest-funding names makes the variance worse, not the edge better. **The carry
thesis — the review's "highest-conviction candidate" — is pruned.** This also
settles B-femr-regime: femr is funding-driven too, so widening it to alts is
unlikely to help; the honest move is to retire it from the live roster absent a
demonstrated G0 PASS.

**Evidence (gate).** `uv run pytest -q` → **116 passed** (+2); `ruff check src
tests scripts` → clean. The only code change is the funding-pagination fix + its
tests (a real data-correctness bug); the carry result is measurement (cache is
gitignored). No strategy/sizing/live-mode change.

**What's next (loop).** With both the TWAP family (B1) and the carry family
(B1-alt) now pruned after costs, the surviving P0 question is whether *any* signal
has edge. Candidates not yet tested on real history: (1) a genuinely
low-frequency basis/cross-sectional momentum that tolerates the 5-min loop;
(2) retire femr from the live roster (B-femr-regime) to stop paying attention to a
dormant/edgeless agent. The negative results are the value here — they've pruned
the two most-hyped theses, so the next iteration should test a *structurally
different* signal rather than re-tuning a known loser.

---

## Iteration 18 — 2026-06-08 — B-mom: cross-sectional momentum has no stable edge (the price-signal thesis flips sign by regime)

**Context.** Iterations 16–17 pruned the two most-hyped theses after costs: TWAP-MR
(B1) and funding carry on majors *and* high-funding alts (B1-alt). PROGRESS's own
"what's next" was explicit: stop re-tuning known losers and **test a structurally
different signal**. The natural candidate is *price* (not funding): cross-sectional
momentum — a classic, capital-scalable, market-neutral crypto edge whose horizon
(hours–days) tolerates the 5-min loop. This iteration built it and ran the honest G0.

**Code change (the committed increment, with tests).**
- **`agents/xsect_momentum.py` — `XSectMomentumAgent` (`xsect_momentum_v1`).**
  Mirrors the cross-sectional carry shape (dollar-neutral, top-K legs, decision-log
  position tracking, maker-friendly patient entries) but ranks on **trailing return**
  over `lookback_bars` (from `view.extra["closes"]`, already supplied per-frame by the
  backtest data builder) instead of funding. LONG the top-K strongest, SHORT the
  bottom-K weakest; exit when a coin leaves the target set, decays below an exit band,
  or its momentum flips sign. A single **`reversion`** flag negates the signal so the
  *same* book tests the opposite thesis (short-horizon cross-sectional mean-reversion:
  long losers / short winners) at zero extra code — efficient for edge-hunting.
- **`tests/test_xsect_momentum.py` (+5):** longs-winner/shorts-loser ranking; the
  `reversion` flag flips both legs; sub-threshold names aren't traded; short series
  → hold; and a continuing-trend backtest stays two-sided, neutral, and positive.
- **`cli/main.py`:** registered the agent in both the `backtest` and `confirm`
  factory dicts so it runs through the standard G0 harness.

**Experiment — confirm on real history (measurement; caches gitignored).** Reused the
two existing 120d/1h caches (majors: BTC/ETH/SOL/HYPE/AVAX/LINK; high-funding alts:
INJ/PURR/TRUMP/AERO/NIL/APT/SPX/PYTH/EIGEN/S). `confirm_strategy(prefer="maker")`,
walk-forward (in = older 70%, oos = recent 30%) + cost ladder, momentum vs reversion:

| universe | variant | in-sample edge/sharpe | oos edge/sharpe | maker full | taker-2x |
|---|---|---|---|---|---|
| majors | momentum  | −4.7 / −2.52 | **+15.8 / +5.55** | +0.8 (1736tr) | −6.7 |
| majors | reversion | **+2.7 / +1.45** | −17.8 / −6.21 | −2.8 (1736tr) | −10.3 |
| alts   | momentum  | −6.3 / −2.91 | **+13.4 / +2.89** | −0.7 (2956tr) | −8.2 |
| alts   | reversion | **+4.3 / +2.00** | −15.5 / −3.16 | −1.4 (2956tr) | −8.9 |

**Evidence — NOT CONFIRMED, all four cells (G0 FAIL).** The result is sharp and
honest: momentum and reversion are exact mirror images (as constructed), and on
*both* universes the cross-sectional price edge **changes sign between the in-sample
and out-of-sample windows.** Momentum lost on the older 70% and won on the recent
30%; reversion did the reverse. That isn't an edge — it's a **regime inversion
mid-window**, which is precisely what walk-forward exists to reject (an edge that
only shows up out-of-sample, with the opposite sign in-sample, is not tradeable
forward). Maker full-sample edge is ≈ flat (+0.8 / −0.7 / −2.8 / −1.4 bps); the taker
ladder is firmly negative everywhere, so even the recent-window momentum would be
eaten by costs at taker.

**Honest conclusion (prunes the third thesis).** Over the last 120d, **no stable
cross-sectional signal — funding (B1/B1-alt) or price-momentum/reversion (B-mom) —
survives walk-forward after costs** on either majors or high-funding alts. The
price-momentum effect is real but *regime-dependent and sign-unstable* on this
window, so a fixed-sign book can't harvest it. This is the third hyped thesis pruned;
the negative result is the value (it stops us deploying a coin-flip).

**Evidence (gate).** `uv run pytest -q` → **121 passed** (+5); `ruff check src tests
scripts` → clean. Committed increment is the agent + tests + CLI registration (pure,
offline); the confirm numbers are measurement (caches gitignored). No
strategy/sizing/live-mode change.

**What's next (loop).** Three structurally different theses (TWAP-MR, carry,
xsect-momentum) are now pruned after costs. Surviving leads, in priority: (1) a
**regime-conditioned** variant — if momentum's sign is regime-dependent, a *timing*
signal (e.g. trade momentum only when a market-wide trend filter is on, flat
otherwise) might stabilize it; cheap to test by gating `xsect_momentum` on an
aggregate-trend condition. (2) **B-femr-regime**: retire femr from the live roster
(dormant + funding-driven, and carry is pruned) to stop attending to an edgeless
agent. (3) A genuinely different *structure* (e.g. event/liquidation microstructure)
rather than another cross-sectional rank. The honest score so far: the chassis is
strong, but no G0 PASS yet — keep pruning until one signal clears walk-forward.


---

## Iteration 19 — 2026-06-08 — B-mom-regime: a regime gate turns alts cross-sectional momentum into the FIRST G0-class lead

**Context.** Iterations 16-18 pruned three theses after costs (TWAP-MR, funding carry,
and plain cross-sectional momentum). Iteration 18's key finding was *specific*:
momentum is real but **sign-unstable** — it lost in-sample (older 70%) and won OOS
(recent 30%), a regime inversion walk-forward correctly rejects. PROGRESS's own
top "what's next" was the matching hypothesis: if momentum's sign is regime-dependent,
a **timing filter** (trade only in a favorable market regime, flat otherwise) might
stabilize it. This iteration built that filter and ran the honest G0.

**Code change (the committed increment, with tests).**
- **`agents/xsect_momentum.py` — causal, default-off `regime_gate`.** Three new config
  knobs (`regime_gate`, `regime_lookback=48`, `regime_min_return=0.0`). When on, the
  agent computes the **equal-weighted mean trailing return of the eligible universe**
  over `regime_lookback` bars (a causal broad-market-trend proxy, from the same
  per-frame `closes`, no look-ahead). If that aggregate is below `regime_min_return`
  (a market drawdown), it **flattens the book and stands aside**; otherwise it trades
  normally. The rationale is a-priori, not fit-to-window: the documented "momentum
  crash" — momentum reverses hardest *after* market bottoms, when the losers you are
  short rebound most — so disabling the book in a bear regime is standard crash
  avoidance. Default-off preserves all prior behavior/tests.
- **`tests/test_xsect_momentum.py` (+2):** the gate stands aside in a market-wide
  drawdown (while the ungated book trades the same dispersion), and re-enables the
  book when the market trends up.

**Experiment — confirm on real history (measurement; caches gitignored).** Reused the
two 120d/1h caches (majors: BTC/ETH/SOL/HYPE/AVAX/LINK; high-funding alts:
INJ/PURR/TRUMP/AERO/NIL/APT/SPX/PYTH/EIGEN/S). `confirm_strategy(prefer="maker")`,
walk-forward + cost ladder, momentum & reversion, gated vs ungated, across
regime_lookback and threshold sweeps.

**Evidence — the gate turns alts momentum tradeable (FIRST G0 PASS).** Base config
(momentum, regime_lookback=12, thr=0) on the alts universe:
| variant | in-sample | oos | maker full | taker-2x | verdict |
|---|---|---|---|---|---|
| momentum **ungated** | −2.6 / −1.09 | +10.0 / +2.25 | +1.6 (2536tr) | −5.9 | sign-flip, FAIL |
| momentum **regime_gate lb=12** | **+4.4 / +1.63** | **+16.0 / +3.38** | **+8.4 (1742tr)** | +0.9 | **PASS** |
The gate removes the in-sample/OOS sign-flip: **both windows are now positive**, OOS
sharpe +3.38, full-sample maker +8.4bps, and even taker-2x is break-even (+0.9). The
gate does real work — it cuts trades 2536→1742 by standing aside in bad regimes and
**raises per-trade maker edge +1.6→+8.4bps**.

**Robustness battery (this is why it's a lead, not noise).**
- **Walk-forward split:** PASS at oos_fraction 0.2/0.3/0.4/0.5 — in +5.5/+4.4/+6.9/+9.5,
  oos +19.2/+16.0/+10.2/+7.7, both windows positive throughout, oos sharpe +2.0→+4.3.
  Not a split artifact.
- **Leave-one-coin-out:** dropping *any* of the 10 alts keeps full-sample maker edge
  **+5.6→+10.4bps** and OOS **+7.8→+20.5bps** in all 10 folds. **No single coin carries
  it** — the edge is basket-wide.

**Honest caveats (keeps this a candidate, not a deploy).**
1. **Alts-only.** Majors momentum in-sample stays negative at every lookback/threshold
   (the regime gate cannot rescue it) — the alts simply have more cross-sectional
   dispersion/momentum to harvest.
2. **Maker-only.** taker-2x hovers ±2bps; the edge lives entirely in maker execution
   (consistent with the whole-project thesis, REVIEW C1). Live maker fill quality matters.
3. **Marginal at the gate.** In-sample edge sits right at the +3bps bar, so the *binary*
   PASS toggles under leave-one-out even though direction/magnitude are stable.
4. **regime_lookback-sensitive.** Only ~12-18-bar regime windows pass; 24+ fail
   in-sample (though all keep OOS strongly positive and maker full-sample positive).

**Conclusion (the first credible engine candidate).** After three pruned theses, the
**regime-gated cross-sectional momentum on high-funding alts is the first signal to
clear G0 in its base config and survive both walk-forward-split and leave-one-coin-out
robustness.** It is not yet a deployable edge — it is alts-only, maker-only, and
marginal at the in-sample gate — but it is the first thing worth *hardening* rather than
pruning. The honest next step is out-of-sample validation it has not had (a fresh time
window + a held-out basket + a parameter-plateau map), filed as **B-mom-regime-validate**.
No strategy/sizing/live-mode change; the committed code is the pure, default-off gate + tests.

**Evidence (gate).** `uv run pytest -q` → **123 passed** (+2); `ruff check src tests
scripts` → clean. Committed increment is the regime gate + 2 tests (pure, offline); all
confirm numbers are measurement (caches gitignored).

**What's next (loop).** Highest priority is now **B-mom-regime-validate** — turn this
lead into a trustworthy (or pruned) G0 by re-confirming on a fresh 120d window and a
disjoint alt basket, and mapping the parameter plateau. If it survives, it is the first
strategy to take through G1→G3 via the existing 5x/1x risk machinery. If it does not
survive a fresh window, it joins the pruned pile — either way the search advances.


---

## Iteration 20 — 2026-06-08 — B-mom-regime-validate: the alts-momentum lead is window-specific (out-of-time FAIL prunes the 4th thesis)

**Context.** Iteration 19 produced the first G0-class lead: regime-gated cross-sectional
momentum on high-funding alts cleared G0 in base config on the trailing 120d (maker full
**+8.4bps**, oos +16.0/sh+3.38) and survived walk-forward-split + leave-one-coin-out.
But it was explicitly flagged a *candidate not a deploy*, pending validation it had not
had. PROGRESS's own top "what's next" — and B-mom-regime-validate — named the decisive
test: **a real edge survives a fresh, disjoint time window.** This iteration built the
capability to fetch one and ran the test.

**Code change (the committed increment, with tests).**
- **`backtest/data.py` — out-of-time window support.** `load_frames` only ever fetched
  the *trailing* `days` (end = now), so there was no way to pull a disjoint older window
  for out-of-sample validation. Added a pure, unit-tested `window_bounds(days, end_ms)`
  helper and threaded an optional `end_ms` through `load_frames` / `cached_or_fetch` /
  `default_cache_path` (a historical window gets an `_end{YYYYMMDD}`-tagged cache file so
  it can't collide with the trailing cache; `end_ms=None` keeps the legacy key, so
  existing caches still resolve). `cli backtest-fetch --end-offset-days N` exposes it.
- **`tests/test_backtest.py` (+2):** `window_bounds` trailing-vs-historical math (an
  older window abuts but never overlaps the trailing one) and `default_cache_path`
  window-keying (trailing keeps the legacy name; a historical end_ms lands in a distinct,
  stable, end-date-tagged file).

**Experiment — validate the lead on real history (measurement; caches gitignored).**
Regime-gated config (regime_gate, regime_lookback=12, thr=0; the Iteration-19 base),
`confirm_strategy(prefer="maker")`, walk-forward + cost ladder:

| test | window / basket | in-sample | oos | maker full | taker-2x | verdict |
|---|---|---|---|---|---|---|
| **sanity (repro Iter-19)** | trailing 120d, orig alts | +4.4 / +1.63 | +16.0 / +3.38 | **+8.4** (1742tr) | +0.9 | ✅ CONFIRMED |
| **(1) out-of-time** | older 120d (ends 2026-02-09), *same* alts | **−7.4 / −3.43** | **−9.4 / −2.95** | **−7.8** (1400tr) | −15.3 | ❌ FAIL |
| **(2) held-out basket** | trailing 120d, disjoint liquid alts | +2.3 / +1.03 | +7.3 / +2.78 | +4.2 (1828tr) | −3.3 | ❌ NOT CONFIRMED |

Held-out basket = SUI/SEI/TIA/WLD/ARB/OP/ENA/JUP/LDO/AAVE (disjoint from the original
INJ/PURR/TRUMP/AERO/NIL/APT/SPX/PYTH/EIGEN/S).

**Evidence — the lead does not survive out-of-time (G0 PRUNE).** The result is sharp and
decisive. The sanity run reproduces Iteration-19 *exactly* (maker full +8.4bps) — so the
new `end_ms` plumbing is consistent and the +8.4 number is real *for that window*. But on
the **immediately-preceding 120d**, the *same* regime-gated agent on the *same* basket
doesn't merely weaken — it **reverses sign** to maker full **−7.8bps** (in −7.4, oos −9.4,
both windows negative, sharpe −3). That is the textbook signature of a window-specific
artifact, not an edge: the trailing-window momentum tailwind simply wasn't present (was
inverted) in the prior period, and the regime gate — which fixed the in/oos sign-flip
*within* the recent window — cannot rescue a period where the cross-sectional momentum
itself is negative. The held-out basket corroborates: on the *recent* window a disjoint
alt set is only marginally positive (in +2.3bps, below the +3 gate) and dies under any
taker slippage, so even the recent-window effect is knife-edge and doesn't generalize
cleanly across baskets. Part (3), the parameter-plateau map, is **moot** — there is no
value in mapping a stable parameter region of a window-specific artifact.

**Honest conclusion (the fourth thesis pruned).** **Regime-gated cross-sectional momentum
is not a durable edge.** Iteration-19's G0 PASS was a property of the specific trailing
120d window, and it fails the one test that matters most — a fresh, disjoint window, where
it flips to −7.8bps maker. This is exactly why out-of-time validation exists, and exactly
why the lead was held back from paper/live. The four structurally-different theses now
pruned after costs: TWAP-MR (B1), funding carry on majors + high-funding alts (B1-alt),
plain cross-sectional momentum (B-mom), and **regime-gated momentum (B-mom-regime)**. The
chassis remains strong; no signal has yet cleared a *trustworthy* (out-of-time) G0. The
negative result is the value — it stopped us deploying a coin-flip dressed as an edge. The
default-off regime-gate code is kept (harmless, and the gate mechanic may still be useful),
but the agent is not a deploy candidate.

**Evidence (gate).** `uv run pytest -q` → **125 passed** (+2); `ruff check src tests
scripts` → clean. Committed increment is the out-of-time fetch capability + 2 tests (pure,
offline); all confirm numbers are measurement (caches gitignored). No strategy/sizing/
live-mode change.

**What's next (loop).** With four theses pruned — including the only G0 lead, now failed
out-of-time — the honest state is: **no signal has a durable, out-of-time-validated edge.**
The out-of-time harness built this iteration is now the *standard bar* every future
candidate must clear (trailing-window G0 is necessary but demonstrably not sufficient).
Priorities: (1) a **structurally different** signal class not yet tested — e.g.
event/liquidation microstructure, or a basis/term-structure trade — rather than another
cross-sectional price/funding rank (three of those are now pruned). (2) Before building
more signals, consider whether the 120d windows are simply too short/regime-dominated for
*any* cross-sectional rank to be stable, in which case longer-horizon or
fundamentally-different structure is required. (3) Retire femr from the live roster
(B-femr-regime) — still dormant + funding-driven, and carry is pruned. Keep pruning until
one signal clears the out-of-time bar.


---

## Iteration 21 — 2026-06-08 — B-mw: the out-of-time bar becomes reusable machinery (`confirm_across_windows`)

**Context.** Iteration 20 pruned the fourth thesis and ended with a sharp lesson: a
strategy can clear the walk-forward + cost-stress G0 gate on the *trailing* 120d and
still **reverse sign** on the immediately-preceding 120d (regime-gated momentum: maker
full **+8.4bps → −7.8bps**). Trailing-window G0 is therefore *necessary but not
sufficient*. That validation was done by hand (refetch an older window, re-run `confirm`,
eyeball the sign). This iteration turns that ad-hoc test into reusable machinery so every
future candidate must clear it — and so a future "G0 PASS" can never again mean "passed on
the one window that happened to look good."

**Code change (the committed increment, with tests).**
- **`backtest/confirm.py` — `confirm_across_windows(factory, windows, *, prefer, **kw)`.**
  Runs `confirm_strategy` on each of N disjoint historical windows
  (`[(label, frames), ...]`) and returns a single `MultiWindowResult` with a
  **DURABLE / NOT DURABLE** verdict. Durable iff **(1)** there are ≥2 windows (one window
  is exactly the Iteration-20 trap), **(2)** *every* window is individually `confirmed`
  (walk-forward + sharpe), and **(3)** the preferred-execution full-sample edge is positive
  in *every* window — i.e. it never flips sign. A sign flip across windows is the textbook
  artifact signature and is called out explicitly in `reasons`
  (`"full-sample edge FLIPS SIGN across windows (… ) — window-specific artifact"`).
- **`preferred_full_sample(cr)` helper** — extracts the cost-ladder rung matching the
  verdict's execution basis (maker, or taker-1x for taker): that is the number whose *sign*
  must be stable. `MultiWindowResult.summary()` prints a per-window ✅/❌ table
  (full / in / oos edge) so the failure mode is legible at a glance.
- **`tests/test_confirm_windows.py` (+3):** (a) an edge that survives two disjoint choppy
  windows → **durable**; (b) mean-reversion that confirms on a choppy window but loses on a
  trend window → **not durable**, with the FLIPS-SIGN reason naming the offending window;
  (c) a single window → **never durable** (the trailing-only trap). All pure/offline.

**Why this and not a fifth signal.** Three of the four pruned theses were cross-sectional
price/funding ranks; the fourth was their regime-gated variant. Before spending another
iteration building a signal that the same hand-run validation would prune, the higher-
leverage move is to *institutionalize* the bar that did the pruning. `confirm_across_windows`
is the gate that would have caught Iteration-19's lead immediately (two windows, sign flip,
NOT DURABLE) instead of after a hand-built out-of-time refetch. It raises the standard
permanently and makes the next signal's evaluation a single, adversarial call.

**Evidence (gate).** `uv run pytest -q` → **128 passed** (+3); `ruff check src tests
scripts` → clean. Committed increment is the harness + helper + 3 tests (pure, offline). No
strategy/sizing/live-mode change; nothing here touches capital.

**What's next (loop).** The durability harness is now the standard bar. Priorities unchanged
from Iteration 20 but now better-equipped: (1) a **structurally different** signal class not
yet tested (event/liquidation microstructure, or basis/term-structure) — and run it through
`confirm_across_windows`, not just a single window. (2) Wire `confirm_across_windows` into the
`hlbot confirm` CLI (fetch trailing + one or more `--end-offset-days` windows and emit the
durability verdict) so the bar is one command, not a script. (3) Retire femr from the live
roster (B-femr-regime). Keep pruning until one signal clears the *multi-window* bar.


---

## Iteration 22 — 2026-06-08 — B-mw-cli: the out-of-time durability bar is now one command (`confirm --windows N`)

**Context.** Iteration 21 built `confirm_across_windows` — the reusable DURABLE /
NOT DURABLE harness that codifies Iteration 20's lesson (a trailing-window G0 PASS
that reverses sign on a disjoint earlier window is a window-specific artifact, not an
edge). But it was library-only: running the bar still meant a hand script (fetch
trailing + each `--end-offset-days` window, call the function). Iteration 21's
next-step (2) was to make the bar a single CLI command so the next candidate's
evaluation is adversarial *by default*. That is this iteration.

**Code change (the committed increment, with tests).**
- **`cli/main.py` — `confirm --windows N`.** When `N>=2`, `confirm` now fetches N
  disjoint, back-to-back `days`-long windows (trailing + N-1 older ones via the
  existing `end_ms` plumbing) and runs `confirm_across_windows`, printing the single
  DURABLE / NOT DURABLE verdict (per-window ✅/❌ table + FLIPS-SIGN reason on
  failure). `--windows 1` (the default) is unchanged — legacy single-window
  PASS/FAIL — so the command is backward-compatible. Exit code is non-zero when not
  durable (so CI / a gate script can branch on it).
- **`_window_specs(windows, days, now_ms)` — pure helper.** Returns the
  `[(label, end_ms), ...]` newest-first window spec: window 0 trails to *now*
  (`end_ms=None`), window i ends `i*days` days earlier so the windows abut without
  overlapping. Extracted out of the command body so the window math is unit-testable
  (repo style: pure functions over inline CLI logic). The CLI fetch loop consumes it.
- **`tests/test_confirm_cli_windows.py` (+2):** (a) single window → trails to now
  (`[("trailing 120d", None)]`); (b) three windows → labels + `end_ms` are disjoint
  and back-to-back (each older window's end == the previous window's start). Pure /
  offline.

**Why this, not a fifth signal.** Three of four pruned theses were cross-sectional
price/funding ranks; the fourth their regime-gated variant. Before spending an
iteration on a signal the same validation would prune, the higher-leverage move was
to finish institutionalizing the bar that did the pruning — making it `hlbot confirm
--windows 2+` instead of a bespoke script. The Iteration-19 lead would have been
caught immediately by `confirm --windows 2` (two windows, sign flip, NOT DURABLE).
The bar is now permanent, legible, and one command; the next signal is tested
adversarially from the first run.

**Evidence (gate).** `uv run pytest -q` → **130 passed** (+2); `ruff check src tests
scripts` → clean. Committed increment is the CLI wiring + pure helper + 2 tests
(pure, offline). No strategy/sizing/live-mode change; nothing here touches capital.

**What's next (loop).** The durability bar is now a single command. Priorities:
(1) a **structurally different** signal class not yet tested (event/liquidation
microstructure, or basis/term-structure) — and run it through `confirm --windows 2+`
from the start, not a single trailing window. (2) Consider whether 120d windows are
too short/regime-dominated for *any* cross-sectional rank to be stable — longer
horizons or fundamentally different structure may be required. (3) Retire femr from
the live roster (B-femr-regime) — still dormant + funding-driven, and carry is pruned.
Keep pruning until one signal clears the multi-window bar.

---

## Iteration 23 — 2026-06-08 — B-tsmom: time-series (absolute) momentum is the fifth thesis pruned (NOT DURABLE, both universes)

**Context.** Four signals are pruned, all *relative* dollar-neutral cross-sectional
ranks: TWAP-MR (B1), funding carry on majors + alts (B1-alt), cross-sectional momentum
(B-mom), and its regime-gated variant (B-mom-regime). The standing next-step across the
last three iterations was a **structurally-different** signal class. The orthogonal axis
to a cross-sectional rank is *time-series (absolute) momentum* — trend-following — where
each coin is traded independently on the sign of its **own** trailing return, so the book
takes **net directional** exposure (all-long in a broad rally, all-short in a sell-off)
instead of washing out beta. It is the single most-documented systematic edge (CTA) and
the one directional strategy that is regime-*adaptive* (it flips short in downtrends).
Whether net-directional trend survives costs *and* a disjoint out-of-time window is exactly
what the Iteration-22 durability bar (`confirm --windows 2+`) exists to judge.

**Code change (the committed increment, with tests).**
- **`agents/ts_momentum.py` — `ts_momentum_v1`.** Per-coin trend signal (no cross-sectional
  ranking): LONG when own `lookback_bars` trailing return ≥ `enter_return`, SHORT when
  ≤ −`enter_return`; exit when |return| < `exit_return`, the trend flips sign, or it leaves
  the band. Strongest trends funded first within the per-trade / total-notional /
  concurrency caps. Maker-friendly (patient entries). A `reversion` flag fades the trend in
  the same book. Volume-gated; reuses the repo's decision-log `_open_positions` replay.
- **Registered** `ts_momentum_v1` in both the `backtest` and `confirm` CLI factories.
- **`tests/test_ts_momentum.py` (+6):** longs up-trend / shorts down-trend / ignores the
  calm name; **takes NET exposure** (two up-trends → two longs, the structural difference
  from the dollar-neutral book); sub-band not traded; `reversion` fades; short series holds;
  books positive maker PnL on a continuing trend.

**Experiment — durability bar from the first run (measurement; caches gitignored).**
`confirm --windows 2 --prefer maker`, walk-forward + cost ladder, two disjoint back-to-back
120d/1h windows:

| universe | window | in-sample | oos | maker full | verdict |
|---|---|---|---|---|---|
| **majors** (BTC,ETH,SOL,HYPE,AVAX,LINK) | trailing 120d | −2.7 | +15.4 | **+2.8** | ❌ not confirmed |
|  | 120d ending 120d ago | −15.4 | +19.5 | **−4.6** | ❌ |
| **high-funding alts** (INJ,PURR,TRUMP,AERO,NIL,APT,SPX,PYTH,EIGEN,S) | trailing 120d | −5.4 | +21.2 | **+2.4** | ❌ not confirmed |
|  | 120d ending 120d ago | −15.8 | +1.9 | **−10.6** | ❌ |

Both universes → **NOT DURABLE** (full-sample edge FLIPS SIGN across windows: majors
+2.8 → −4.6bps, alts +2.4 → −10.6bps).

**Evidence — pruned, and pruned for the same reason as B-mom.** The result is decisive and
structurally informative. On the trailing window the strategy looks marginally positive on
the full sample (+2.8 / +2.4bps) but is **negative in-sample** (−2.7 / −5.4) with a strongly
positive OOS (+15.4 / +21.2) — i.e. it doesn't even *confirm* within the recent window; the
positive full-sample number is carried entirely by a mid-window regime inversion (the same
in→oos sign-flip that sank plain cross-sectional momentum in B-mom). The
immediately-preceding 120d then flips the full-sample edge firmly negative (−4.6 / −10.6bps).
That is the textbook window-specific-artifact signature, and the durability bar names it
explicitly. Net-directional trend over disjoint 120d windows is, as suspected, largely a bet
on each window's regime — it does not carry a cost-surviving edge across regimes here.

**Honest conclusion (the fifth thesis pruned).** **Time-series momentum is not a durable
edge** on either majors or high-funding alts at the 1h/120d horizon, after maker costs and
the out-of-time bar. The five structurally-different theses now pruned: TWAP-MR, funding
carry (majors + alts), cross-sectional momentum, regime-gated cross-sectional momentum, and
**time-series momentum** — i.e. *both* the relative (dollar-neutral) and the absolute
(directional) momentum classes fail the same disjoint-window test, in the same way (a
trailing-window artifact that reverses on the prior window). This sharpens the meta-lesson
from Iteration 20/22: at the 1h/120d evaluation horizon, **price-return momentum in any form
is regime-dominated** — the window's trend, not a persistent edge, is what the backtest
measures. The chassis remains strong; no signal has cleared the multi-window bar. Negative
result is the value: it stopped a CTA-style directional book from reaching paper on the
strength of a single flattering window. No strategy/sizing/live-mode change.

**Evidence (gate).** `uv run pytest -q` → **136 passed** (+6); `ruff check src tests
scripts` → clean. Committed increment is the agent + registration + 6 tests (pure, offline);
all confirm numbers are measurement (caches gitignored).

**What's next (loop).** Five momentum/carry theses pruned; the honest read is that
*price-return rank/trend at 1h/120d is regime-dominated and unlikely to yield a durable
edge in any further variant.* The higher-leverage moves are now signals that are **not**
price-return derivatives: (1) **microstructure / event** signals (liquidation-cascade
follow-through, order-flow imbalance) — these need a tick/WS replay the offline candle
harness can't yet provide, so the slice is *building that data path* (a fills/trades replay
into `Frame`), then testing through `confirm --windows 2+`; (2) **basis / term-structure**
(perp-vs-spot or funding term structure) — a genuinely different economic driver, though
M5 flags the spot-scaling as fragile; (3) consider a **longer evaluation horizon** (4h/1d
bars, >120d) to test whether the regime-domination is a horizon artifact before abandoning
cross-sectional structure entirely. (4) Retire femr from the live roster (B-femr-regime).
Keep pruning until one signal clears the multi-window bar.

---

## Iteration 24 — 2026-06-08 — 429-resilient fetch unblocks the longer-horizon test; majors xsect-momentum at 1d/240d is the first taker-survivable, non-sign-flipping lead

**Context.** Five momentum/carry theses are pruned, all at the **1h/120d** horizon, and the
standing meta-lesson (Iteration 20/22/23) was that *price-return momentum in any form is
regime-dominated at 1h/120d* — every prune showed the same signature (maker-only edge, taker
negative, and a full-sample **sign flip** across disjoint windows). The explicit open question
across the last three iterations: **is the regime-domination a horizon artifact?** The
`confirm`/`backtest` CLIs already accept `--interval {4h,1d}`, and the data layer already scales
funding per-bar, so the longer-horizon test needed *no new strategy code* — just the ability to
fetch a longer/larger window without dying on the rate limiter.

**Code change (the committed increment, with tests).**
- **429-resilient fetch.** `fetch_candles` and `_fetch_funding_page` now route their POST
  through a new `_request_with_retry(do_request, …)` that retries 429 + transient 5xx with
  exponential backoff (honoring a `Retry-After` header), capped at `max_delay`. A longer/larger
  backtest window means many more candle requests **and** many more funding pages (1d/240d ≈ 12
  funding pages/coin), which reliably tripped HL's limiter — and a 429 mid-window used to lose
  the *whole* window's progress (cache is per-completed-window). The helper is pure given
  `do_request` + `sleep` (no network/real clock), so it is unit-tested with a fake response
  sequence: first-success → no backoff; 429→503→200 → recovers with growing delays; `Retry-After`
  header overrides backoff; exhausted retries → surfaces `HTTPStatusError`.
- **`tests/test_backtest.py` (+4):** the four cases above. Pure / offline.

**Experiment — the longer-horizon durability test (measurement; caches gitignored).**
`confirm --agent xsect_momentum_v1 --interval 1d --days 240 --windows 2 --prefer maker`, two
disjoint back-to-back 240d/1d windows, walk-forward + full cost ladder:

| universe | window | full | in | oos | cost ladder (full) |
|---|---|---|---|---|---|
| **majors** (BTC,ETH,SOL,HYPE,AVAX,LINK) | trailing 240d | **+18.0** | −1.4 | +52.8 | maker +18.0 / **taker-1x +12.5 / 2x +10.5 / 3x +8.5** (robust-to-2x-slip ✅) |
|  | 240d ending 240d ago | **+69.0** | +110.9 | +20.9 | — |
| **high-funding alts** (INJ,PURR,TRUMP,AERO,NIL,APT,SPX,PYTH,EIGEN,S) | trailing 240d | −50.4 | −90.7 | +26.3 | — |
|  | 240d ending 240d ago | −122.0 | −72.9 | −183.3 | — |

**Evidence — a qualitatively new result on majors, and the opposite on alts.** The bar's
verdict is still **NOT DURABLE** for majors — but for a *different, much weaker* reason than the
five prunes. On majors at 1d:
1. **No sign flip.** Full-sample edge is **positive in both disjoint windows** (+18.0 and
   +69.0bps). Every 1h/120d prune flipped sign across windows — that was the artifact signature.
   It is absent here.
2. **Taker-survivable.** The trailing window is **positive across the entire cost ladder**
   (maker +18.0 → taker-3x +8.5bps, "robust to 2x slippage: True"). Every prior signal was
   maker-only with taker firmly negative. This is the **first** signal in the whole search that
   nets positive *as a taker*.
3. **Blocked only at the per-window gate.** The single thing failing G0 is the trailing window's
   **in-sample half** (−1.4bps < the +3 bar); its OOS is +52.8bps/sh+2.12 and the *older* window
   is strongly positive in both halves (in +110.9 / oos +20.9). So this is gate-marginal in one
   half of one window — not the structural sign-reversal that pruned the others.

On **alts** at 1d the signal is **strongly negative** (full −50.4 / −122.0bps) — the longer
horizon does **not** rescue alts; if anything it's worse than the 1h alts result. (Caveat: a few
alts in the basket — TRUMP/NIL/SPX — may have <240d of listed history, which can distort the
older window; the trailing-window negativity is not explained by that.)

**Honest conclusion (a lead, not an edge — but the best lead so far).** The meta-lesson needs
amending: *price-return momentum is regime-dominated **at 1h/120d**, but on **majors at the 1d
horizon** cross-sectional momentum stops sign-flipping and survives the full cost ladder.* This
is the first candidate that fails the durability bar **only** on a gate-marginal in-sample half
rather than on the artifact sign-flip — i.e. the first signal worth *trying to push over* G0
instead of pruning. It is **not** a confirmed edge and nothing here touches capital: NOT DURABLE
stands, it's majors-only, and it's blocked by a real (if marginal) in-sample weakness. Recorded
as a P0 lead (B-horizon) to investigate next, not as a pass.

**Evidence (gate).** `uv run pytest -q` → **140 passed** (+4); `ruff check src tests scripts` →
clean. Committed increment is the retry helper + 4 tests (pure, offline); all confirm numbers are
measurement (caches gitignored). No strategy/sizing/live-mode change.

**What's next (loop).** Push the majors-1d lead toward G0 (B-horizon): (1) **lookback sweep** at
1d — the default `lookback_bars` is 1h-tuned; the trailing in-sample is only −1.4bps, so a
horizon-appropriate lookback may lift it over +3 and confirm the window. (2) **More/longer
windows** — run `--windows 3` at 1d to see if the no-sign-flip property holds over 3 disjoint
240d windows (≈2yr of majors history exists). (3) **Regime-gate at 1d** — the Iteration-19 gate
helped the recent window; re-test it at the daily horizon where the base signal is already
taker-survivable. (4) If a 1d lookback confirms both windows on majors across the cost ladder,
that is the **first G0-class candidate** — then widen the majors basket and stress more windows
before any paper talk. Alts at 1d are pruned; this is a majors-only thread.

---

## Iteration 25 — 2026-06-08 — B-horizon slice 1: the 1d lookback sweep finds a 12–15-bar plateau that CONFIRMS the trailing window and stops sign-flipping — but the durability bar still says NOT DURABLE (older window's walk-forward fails)

**Context.** Iteration 24 found the first non-prune lead: majors `xsect_momentum_v1` at **1d/240d**
is the only signal that (a) doesn't sign-flip across two disjoint windows and (b) survives the full
taker cost ladder — blocked from G0 *only* by the trailing window's gate-marginal in-sample half
(−1.4bps < +3 at the default `lookback_bars=24`). The default lookback is **1h-tuned**; at the daily
horizon a 24-bar lookback is 24 *days*, almost certainly too long. Slice (1) of B-horizon was a 1d
lookback sweep to lift that in-sample half. But the `confirm`/`backtest` factories **hardcoded
`config={}`**, so a sweep meant editing code each time — the tooling gap had to close first.

**Code change (the committed increment, with tests).**
- **`--params` config override on `confirm` and `backtest`.** New `_parse_agent_params(str)` pure
  helper parses a `key=value,key=value` CLI string into a typed agent-config dict, inferring
  **int → float → bool → str** (so `lookback_bars=14` is an int, `enter_return=0.05` a float,
  `reversion=true` a bool); empty string → `{}`; missing-`=`/empty-key raise. A `_coerce_param`
  inner does the per-value inference. Both CLIs parse `--params` once and thread the dict through
  every agent factory (`config=cfg` instead of `config={}`), so any candidate's config is now
  sweepable without a code edit. The factories' previous hardcoded `{}` was the only thing
  standing between the durability harness and a parameter sweep.
- **`tests/test_parse_agent_params.py` (+6):** empty→`{}`; int/float/bool/str inference with type
  assertions; whitespace tolerated; negative numbers; missing-`=` raises; empty-key raises. Pure,
  offline.

**Experiment — the 1d lookback sweep on majors (measurement; caches gitignored).**
`confirm --agent xsect_momentum_v1 --coins BTC,ETH,SOL,HYPE,AVAX,LINK --interval 1d --days 240
--prefer maker --params lookback_bars=N`. Trailing 240d window, walk-forward + cost ladder:

| lookback_bars | in-sample | oos | maker full | taker-3x | verdict |
|---|---|---|---|---|---|
| 5  | −38.0 | +50.4 | −13.9 | −23.4 | ❌ |
| 7  | −24.4 | +41.2 | −2.4  | −11.9 | ❌ |
| 10 | +0.4  | +43.4 | +16.5 | +7.0  | ❌ (in <+3) |
| 12 | +33.8 | +31.6 | +34.5 | —     | ✅ CONFIRMED |
| 13 | +47.9 | +47.6 | +47.0 | —     | ✅ CONFIRMED |
| **14** | **+49.1** | **+42.7** | **+46.2** | **+36.7** | ✅ **CONFIRMED** |
| 15 | +29.6 | +36.5 | +31.1 | —     | ✅ CONFIRMED |
| 16 | +36.3 | +20.1 | +30.0 | —     | ❌ (oos sharpe) |
| 20 | −13.4 | +19.8 | −3.8  | −13.3 | ❌ |
| 24 (default) | −1.4 | +52.8 | +18.0 | +8.5 | ❌ (in <+3) |

So the sweep did exactly what slice (1) hoped: a **coherent 12–15-bar plateau** (≈2 weeks of daily
lookback) **CONFIRMS** the trailing window — not a knife-edge single point — with in & oos both
~+30–49bps/sharpe ~+1.0–1.5, and at lb=14 the edge is **positive across the entire cost ladder**
(maker +46.2 → taker-3x +36.7bps). The default 24-bar lookback was simply the wrong horizon.

**Experiment — the durability bar (`--windows 2`), the decisive test.**
`… --windows 2 --params lookback_bars={13,14}`, two disjoint back-to-back 240d/1d windows:

| lookback | window | full | in | oos | window verdict |
|---|---|---|---|---|---|
| 13 | trailing 240d | +47.0 | +47.9 | +47.6 | ✅ confirmed |
|    | 240d ending 240d ago | **+16.0** | +55.5 | −54.4 | ❌ |
| 14 | trailing 240d | +46.2 | +49.1 | +42.7 | ✅ confirmed |
|    | 240d ending 240d ago | **+8.3** | +55.6 | −94.2 | ❌ |

Both → **NOT DURABLE.**

**Evidence — NOT DURABLE, but for a genuinely new (and weaker) reason than the five prunes.** The
key, honest distinction: the full-sample edge is **positive in BOTH disjoint windows** (lb14 +46.2
& +8.3bps; lb13 +47.0 & +16.0bps) — i.e. **no sign flip.** Every one of the five 1h/120d prunes
flipped the full-sample sign across windows; that artifact signature is **absent here.** What blocks
durability is the *older* window's **walk-forward**: its in-sample is strongly positive (+55.5/+55.6)
but its OOS tail **reverses hard** (−54.4 / −94.2bps), so that window isn't individually confirmed
(durability requires *every* window confirmed, not just a stable sign). In plain terms: the most
recent ~72d of the older 240d window had a momentum-crash-like reversal that the walk-forward
correctly refuses to ignore — the same kind of regime inversion that the Iteration-19 regime gate
was built to stand aside from.

**Honest conclusion (the lead persists, still not an edge).** Slice (1) succeeded as research: a
horizon-appropriate lookback (12–15 daily bars) turns the trailing-window in-sample from −1.4 to
~+45bps and CONFIRMS it across the full cost ladder — the strongest single-window result of the
whole search, and on a *plateau*, not a knife-edge. But the durability bar still says **NOT
DURABLE**: the older 240d window fails its own walk-forward (OOS-tail reversal). This is materially
better than the five prunes (no sign flip; taker-survivable; a coherent plateau) yet honestly short
of G0 — so **nothing here touches capital** and the lead stays a P0 candidate, not a pass. The
amended meta-lesson: *majors 1d cross-sectional momentum at a ~2-week lookback is a real,
cost-surviving, sign-stable signal — but it is still vulnerable to a momentum-crash window, which is
exactly what a causal regime gate (next slice) exists to handle.*

**Evidence (gate).** `uv run pytest -q` → **146 passed** (+6); `ruff check src tests scripts` →
clean. Committed increment is the `--params` flag + pure parser + 6 tests (pure, offline); all
confirm numbers are measurement (caches gitignored). No strategy/sizing/live-mode change.

**What's next (loop).** Push the lead with B-horizon's remaining slices, in order: (1) **regime-gate
at 1d** — the older window's *in-sample* is strongly + and only its OOS tail reverses; the
Iteration-19 causal `regime_gate` (now sweepable via `--params regime_gate=true,regime_lookback=…`)
may stand the book aside during that crash and confirm the older window. This is the highest-leverage
next move because the failure mode is exactly what the gate targets. (2) **`--windows 3` + widen the
majors basket** to test whether the plateau and the no-sign-flip property survive more history/coins.
(3) **longer `--days`** per window so the OOS tail is a smaller fraction of each window. If the
regime gate confirms both (then three) windows across the cost ladder → the first G0-class candidate,
and only then widen + stress before any paper talk. Alts at 1d stay pruned; majors-only.

---

## Iteration 26 — 2026-06-08 — B-horizon slices 2 & 3: the regime gate can't rescue the older 1d window; momentum is sign-stable across 3 disjoint windows yet fails each window's walk-forward — harness now names this failure mode (sign-stable lead ≠ artifact)

**Context.** Iteration 25 found the strongest non-prune lead of the whole search: majors
`xsect_momentum_v1` at **1d**, lookback 12–15 bars, CONFIRMS the trailing 240d window across the
full cost ladder and — uniquely among every candidate — **does not sign-flip** across two disjoint
windows. It fails `--windows 2` only on the *older* window's walk-forward (in +55.6 / oos −94.2bps:
an OOS-tail momentum-crash reversal). The backlog laid out the next slices: (2) regime-gate at 1d,
(3) `--windows 3` + widen basket, (4) longer `--days`. This iteration ran (2) and (3) as
measurement and shipped the tested code increment they motivated.

**Experiment — slice (2): regime-gate at 1d (measurement; caches gitignored).** The Iteration-19
causal `regime_gate` stands the dollar-neutral book aside when the equal-weighted universe's
trailing return over `regime_lookback` bars is < `regime_min_return`. It was built precisely for an
OOS-tail reversal, and the `--params` flag makes it sweepable. Ran `confirm --windows 2 --prefer
maker --params lookback_bars=14,regime_gate=true,regime_lookback=N`:

| regime_lookback | trailing full / in / oos | older full / in / oos | verdict |
|---|---|---|---|
| 12 | −4.2 / −0.4 / −9.0 ❌ | +61.1 / +139.9 / −65.0 ❌ | NOT DURABLE + **sign FLIP** |
| 24 | +60.0 / +44.9 / +73.0 ✅ | +113.3 / +211.7 / **−16.3** ❌ | NOT DURABLE |
| 36 | +16.7 / +5.9 / +23.3 ✅ | +74.7 / +183.0 / −56.8 ❌ | NOT DURABLE |
| 48 (default) | +34.1 / −17.6 / +49.4 ❌ | +47.5 / +94.5 / −27.5 ❌ | NOT DURABLE |

**The gate does NOT rescue the older window at any setting.** It lifts the older window's *in-sample*
massively (rl24: +55.6 → +211.7bps) but its **OOS tail still reverses** (−16.3 to −65.0bps), so the
window stays unconfirmed; and rl12 even *breaks* the previously-confirmed trailing window into a
sign flip. The reason is structural: a momentum crash happens on a **market rebound** (the short-leg
losers rebound hardest as the market turns *up* off a bottom), so a "stand aside when the market is
*down*" filter is looking the wrong way — the crash is in a positive-return regime the gate keeps
trading. Slice (2) is a clean negative: the existing regime gate is the wrong tool for this failure.

**Experiment — slice (3): `--windows 3` (measurement).** `confirm --windows 3 --prefer maker
--params lookback_bars=14` (three disjoint, back-to-back 240d/1d windows, ~2yr of majors history):

| window | full | in | oos | confirmed |
|---|---|---|---|---|
| trailing 240d | **+46.2** | +49.1 | +42.7 | ✅ |
| 240d ending 240d ago | **+8.3** | +55.6 | −94.2 | ❌ |
| 240d ending 480d ago | **+11.6** | −0.6 | +59.1 | ❌ |

The full-sample maker edge is **positive in all three** disjoint windows — **no sign flip over ~2
years**, stronger evidence of a real directional signal than the 2-window run. But windows 2 & 3
each fail their *own* walk-forward (each has a regime inversion in one half: window 2 in its OOS
tail, window 3 in its in-sample half). So the signal is **sign-stable but not every-window-confirmed**
— a qualitatively different state from the five pruned theses, every one of which sign-*flipped*.

**Code change (the committed increment, with tests).** The durability harness lumped these two
NOT-DURABLE failure modes together: it surfaced the **sign-flip** ("window-specific artifact") but
had no name for **sign-stable-but-walk-forward-blocked** — the exact, decision-relevant state this
lead is in. Added to `backtest/confirm.py`:
- **`MultiWindowResult.sign_stable: bool`** — True iff every window's preferred-execution
  full-sample edge is present and shares one sign (no flip). Computed alongside the existing
  `all_positive`/sign-flip logic; `durable` implies `sign_stable`.
- **An explicit triage NOTE** in `reasons` when a run is NOT durable yet sign-stable, all-positive,
  and blocked only by within-window walk-forward: *"full-sample edge is positive and sign-stable
  across all N windows — blocked only by within-window walk-forward, i.e. regime-sensitive, not the
  cross-window artifact signature (a lead to push, not an artifact to discard)."* This fires on the
  real majors-1d data and is mutually exclusive with the sign-flip reason.
- **`tests/test_confirm_windows.py` (+1 fn, edits):** a sign-stable-but-unconfirmed scenario (two
  choppy windows, one too small to clear an unreachable sharpe bar) asserts `sign_stable` True, no
  FLIPS-SIGN reason, and the regime-sensitive NOTE present; the existing sign-flip test now asserts
  `sign_stable` False and that the NOTE does **not** fire on a genuine artifact; the durable test
  asserts durable ⇒ sign_stable.

**Honest conclusion.** The lead persists and is now better-characterized, not advanced past G0. Two
results: (a) the existing regime gate **cannot** fix the failure — it targets drawdowns, but the
failure is a rebound momentum-crash — so slice (2) is pruned as a fix; (b) the signal is
**sign-stable across 3 disjoint windows (~2yr)**, which the harness now reports as a distinct,
push-worthy state rather than burying under a bare NOT DURABLE. **NOT DURABLE still stands; nothing
touches capital; majors-only.** The meta-lesson is refined: *the durability bar has two distinct
failure modes — a cross-window sign flip (artifact, discard) and within-window walk-forward failure
with a stable cross-window sign (regime-sensitive lead, keep pushing). Conflating them wastes the
signal.* This candidate is firmly the second kind.

**Evidence (gate).** `uv run pytest -q` → **147 passed** (+1); `ruff check src tests scripts` →
clean. Committed increment is the `sign_stable` diagnostic + NOTE + tests (pure, offline); all
confirm numbers are measurement (caches gitignored). No strategy/sizing/live-mode change.

**What's next (loop).** B-horizon remaining slices, now that the regime-gate fix is ruled out:
(4) **longer per-window `--days`** (e.g. 360–480) so each window's OOS tail is a smaller fraction —
tests whether windows 2 & 3's walk-forward failures are boundary/fraction artifacts of the 240d cut
rather than genuine regime breaks; (5) **widen the majors basket** (add DOGE/XRP/LTC/BNB/etc.) to
see if the plateau and the (now 3-window) no-sign-flip property strengthen with more cross-sectional
breadth. If a longer-window or wider-basket run gets *every* window individually confirmed while
keeping the sign-stable property → first G0-class candidate. Alts at 1d stay pruned; majors-only.

---

## Iteration 27 — 2026-06-08 — B-horizon PRUNED: longer windows don't shrink the walk-forward failure, wider baskets break sign-stability; shipped a canonical named-basket resolver for reproducible/honest universes

**Context.** Iteration 26 left the majors-1d cross-sectional-momentum lead in its best-characterized
non-durable state: at lb=14 it CONFIRMS the trailing 240d window across the full cost ladder and is
**sign-stable** across 3 disjoint windows (~2yr, no flip), failing `--windows 2/3` only on each older
window's *within-window* walk-forward (a regime inversion in one half). Two slices remained: (4) longer
per-window `--days` so each window's OOS tail is a smaller fraction (boundary-artifact test), and
(5) widen the majors basket (breadth test). This iteration ran both as measurement and shipped the
tested code increment the arc motivated.

**Experiment — slice (4): longer `--days` (measurement; caches gitignored).**
`confirm --agent xsect_momentum_v1 --coins majors --interval 1d --days 360 --windows 2 --prefer maker
--params lookback_bars=14`:

| window | full | in | oos | confirmed |
|---|---|---|---|---|
| trailing 360d | **+20.2** | +27.3 | +5.0 | ❌ (oos sharpe +0.15 < 1.0) |
| 360d ending 360d ago | **+26.7** | −20.3 | +104.6 | ❌ (in-sample negative) |

Full-sample is **positive and sign-stable in both** (the harness NOTE fires: regime-sensitive lead, not
the artifact). But lengthening the window **did not** shrink the walk-forward failure — the regime
inversion simply **relocated to a different half** (trailing: weak OOS tail; older: negative *in-sample*,
huge OOS). So the OOS-tail-fraction / boundary-artifact hypothesis is **disproven**: the within-window
failure is intrinsic to the signal, not an artifact of the 240d cut.

**Experiment — slice (5): widen the majors basket (measurement).**
`confirm --coins majors_wide --interval 1d --days 240 --windows 3 --prefer maker --params lookback_bars=14`
(12 coins: BTC,ETH,SOL,HYPE,DOGE,XRP,LTC,BNB,AVAX,LINK,SUI,AAVE):

| window | full | confirmed |
|---|---|---|
| trailing 240d | **+21.8** | ❌ |
| 240d ending 240d ago | **−3.4** | ❌ |
| 240d ending 480d ago | **+133.3** | ❌ |

The wider basket is **actively worse**: full-sample now **FLIPS SIGN** (+133.3 … −3.4bps), the artifact
signature the harness flags — and the trailing window's edge fell vs the 4-coin run (+46.2 → +21.8).
Breadth **breaks** the one good property (sign-stability) the narrow basket had. More coins ≠ more edge
here; the cross-section gets noisier, not cleaner.

**Conclusion — the lead is pruned (sixth thesis).** majors-1d cross-sectional momentum at a ~2-week
lookback is a genuine, cost-surviving, sign-stable directional signal *on the trailing narrow basket* —
materially better than the five sign-flipping prunes — but it is **regime-sensitive within every window**
and never clears the durability bar across **any** axis tried: lookback plateau (Iter 25), regime gate
(Iter 26), `--windows 3` (Iter 26), longer window length (slice 4), or basket breadth (slice 5). The
within-window walk-forward failure is intrinsic, not a boundary or breadth artifact. Honest call: **NOT
DURABLE, no G0, nothing touches capital, majors-only.** The meta-lesson stands reinforced: a
trailing-window PASS plus cross-window sign-stability is necessary but *still* not sufficient — durability
needs *every* window individually walk-forward-confirmed, and this signal can't deliver that.

**Code change (the committed increment, with tests).** The arc kept hand-typing baskets (majors, the
high-funding alts, the held-out alts, now majors_wide) on every confirm/backtest — and a single typo
silently changes a recorded number, a real honesty risk for a track-record-grade search. New pure
`src/hl_bot/backtest/baskets.py`:
- **`BASKETS`** — the search's universes pinned to version-controlled names (`majors`, `majors6`,
  `majors_wide`, `alts_highfunding`, `alts_heldout`), each annotated with the iteration that used it, so
  every cited result is reproducible.
- **`resolve_basket(spec)`** — expands preset names and passes bare symbols through, order-preserving and
  deduped. Backward compatible: `--coins BTC,ETH` unchanged, `--coins majors` expands, `--coins majors,DOGE`
  mixes the two. Names are lower_snake, symbols UPPER, so a token is unambiguously one or the other.
- Wired into `confirm` / `backtest` / `backtest-fetch` (the three research commands; **live tick paths
  left untouched** to keep zero live-path risk).
- **`tests/test_baskets.py` (+7):** pass-through, preset expansion, case-insensitive names, mix+dedupe
  order-preserving, uppercasing, empty/whitespace tolerance, two-preset concatenation.

Smoke-tested offline: `confirm --coins majors … --windows 1 --cache` resolves to the cached
BTC-ETH-HYPE-SOL dataset and reproduces slice (4)'s trailing +20.2bps (no network).

**Evidence (gate).** `uv run pytest -q` → **154 passed** (+7); `ruff check src tests scripts` → clean.
Committed increment is the basket resolver + wiring + 7 tests (pure, offline); all confirm numbers are
measurement (caches gitignored). No strategy/sizing/live-mode change.

**What's next (loop).** B-horizon is exhausted; the six structurally-different theses (twap_mr, carry
x2, xsect-momentum + reversion, regime-gated momentum, ts-momentum, and now the 1d horizon variant) are
all pruned after the out-of-time bar. The honest state: **no agent passes G0 on any universe tried.**
Candidate next directions, in rough priority: (a) **B-femr-regime** — retire femr from the live roster
(it's dormant on majors and carry has no edge even on high-funding alts), a clean honesty/hygiene win;
(b) a genuinely *new signal class* not yet tried — e.g. **basis/term-structure** or an **intraday
microstructure** edge at a cadence the 5-min loop can actually capture (REVIEW C7) — since every
daily/hourly price-momentum and funding-carry variant is now pruned; (c) revisit whether the durability
bar itself should accept a **regime-conditional** deployment (only trade the signal in its confirmed
regime) rather than demanding unconditional every-window confirmation — but that needs a *causal* regime
detector that the Iter-26 drawdown gate already failed to provide, so it's research, not a quick win.

---

## Iteration 28 — 2026-06-08 — B-femr-regime DONE: femr retired from the live roster (auditable hard-block), kept paper-evaluated

**Context.** Six structurally-different theses are now pruned after the out-of-time durability bar
(Iter 27); no agent passes G0 on any universe tried. The top unchecked P0 backlog item was
**B-femr-regime** — a clean honesty/hygiene win: femr is dormant on majors (its 130%-APR funding
entry never trips on liquid coins, B1) and funding *carry* has no net-of-cost edge even on
high-funding alts (B1-alt), so widening the same funding-driven thesis to alts can't help. The
honest move is to retire femr from the *live* roster until a universe+variant earns a G0 PASS.

**Change (tightening-only, with tests).** Added `RETIRED_LIVE_AGENTS` to `cli/main.py`: a documented
name→reason registry of agents that may keep evaluating in **paper** (for ongoing measurement) but
are **hard-blocked from the live execution roster regardless of `agent_state`**. `_filter_live_agents_by_state`
now skips any retired agent up front with its audit reason, so even an accidental `live_small`/`live`
promotion of femr can no longer place a live order. `femr_v1` is the sole entry, annotated with the
B1/B1-alt evidence. femr stays in the paper roster (line ~400) so it keeps producing scorecards; only
its path to *capital* is closed. Consistent with the hard rule that risk changes are tightening-only.

- **`tests/test_live_agent_state.py`:** added `test_retired_agent_blocked_from_live_regardless_of_state`
  — femr promoted to `live`/enabled still yields an empty live roster and the retirement skip reason.
  Re-pointed the existing `test_live_roster_requires_enabled_live_mode` off femr (now retired) onto
  `twap_mr_regime_v1` so it still asserts the enabled+live_small gate for a non-retired agent.

**Why this and not new-edge work.** It was the highest-priority *unblocked* backlog item and a pure
hygiene/honesty win with zero edge-discovery risk: an honest live roster shouldn't carry a dormant,
edgeless agent that could be promoted by mistake. New-signal-class work (basis/term-structure,
intraday microstructure) remains the higher-leverage but larger next direction.

**Evidence (gate).** `uv run pytest -q` → **155 passed** (+1); `ruff check src tests scripts` → clean.
No strategy/sizing change; no live mode enabled (the opposite — femr's live path is closed). No secrets.

**What's next (loop).** With the price-momentum and funding-carry families exhausted and femr retired,
the honest frontier is a **genuinely new signal class** the 5-min loop can capture: (a) **basis /
term-structure** (perp-vs-spot or funding term structure as a carry signal distinct from raw funding
level — note M5 flags the spot-scaling fragility, so it needs a careful data path); (b) an **intraday
microstructure** edge at WS cadence (REVIEW C7) rather than the daily/hourly bars every pruned thesis
used. Lower-priority hygiene: liq_cascade is similarly dormant unless WS-fed (B11) and is a retirement
candidate by the same standard if it stays edgeless.

---

## Iteration 29 — 2026-06-08 — B-pairs slice 1: pairs/relative-value mean-reversion — THE FIRST SIGNAL TO CLEAR THE CANONICAL DURABILITY BAR (maker, 120d×2 windows)

**Context.** Six structurally-different *bar-based* theses are pruned after the out-of-time durability
bar (twap_mr, funding/xfund carry, cross-sectional momentum + reversion, regime-gated momentum,
ts-momentum, and the 1d-horizon momentum variant). femr is retired (Iter 28). Every one of them keyed
off a coin's **own** trailing return (momentum) or its **own** funding level (carry), and every one
failed the same way: a sign-flip between in-sample and OOS, or across disjoint windows. The honest
frontier (Iter 28 "what's next") was a **genuinely new signal class**. This iteration builds the
seventh thesis and runs it through the bar from the first run.

**The new thesis — pairs / relative-value statistical arbitrage.** New `pairs_reversion_v1`
(`agents/pairs_reversion.py`): market-neutral, trades the **log-price-ratio spread** of an
economically-related coin pair against its rolling-z mean. When `z = (spread − rolling_mean)/rolling_std`
is extreme (|z| ≥ entry_z), SHORT the rich leg and LONG the cheap leg in **equal dollars**; hold until
the spread reverts inside the band (`entry_sign·z ≤ exit_z`, which covers both reversion-to-zero and a
flip clean through the mean), then flatten **both legs together**. Default pairs ETH/BTC, SOL/AVAX,
LINK/AAVE (coin-disjoint so per-coin position tracking is unambiguous); pairs overridable from
`--params` via an `'A/B|C/D'` string. This is **orthogonal to all six pruned theses**: it keys off the
*relationship between two coins*, not either coin's own return or funding — a pairwise
cointegration/mean-reversion, the classic relative-value edge class never tried here. Maker-friendly
(patient entries), hours-horizon (tolerates the 5-min loop).

**Code increment (committed, with tests).** Agent + registration in the `confirm`/`backtest` factories
(research commands only; **live tick paths untouched**, zero live-path risk). `tests/test_pairs_reversion.py`
(+7): stretched-up → short-rich/long-cheap with equal-dollar legs; stretched-down mirror; spread-at-mean
not traded; too-short history holds; an open pair flattens **both** legs on reversion; `_parse_pairs`
string-form (upper-cases, drops self-pairs/malformed); and a backtest that books **positive maker PnL**
when a one-bar dislocation reverts (short the rich leg at the extreme, cover at par). Pure/offline.

**Evidence — real HL history (network, caches NOT written; `--no-cache`).** 6 coins
(BTC,ETH,SOL,AVAX,LINK,AAVE), 1h, default 3 pairs, lb=48, `--prefer maker`:

*Single window, trailing 120d (2881 frames):*

| exec | full edge | sharpe | trades |
|---|---|---|---|
| maker | **+5.3bps** | +2.72 | 760 |
| taker-1x | −0.2 | −0.08 | 760 |
| taker-2x | −2.2 | −1.11 | 760 |

walk-forward maker: **in +6.1bps/sh+2.94, oos +3.4bps/sh+2.19** — *both halves positive*. Every one of
the six pruned theses sign-flipped in→oos on its trailing window; **this is the first that does not.**

*Durability bar `--windows 2`, 120d (the canonical standard since Iter 21):*

| window | full | in | oos | confirmed |
|---|---|---|---|---|
| trailing 120d | +5.3 | +6.1 | +3.4 | ✅ |
| 120d ending 120d ago | +8.0 | +7.1 | +9.7 | ✅ |

→ **✅ DURABLE.** Both disjoint windows individually walk-forward-confirmed, in+oos positive in each, no
sign flip. **No prior thesis ever passed this bar.**

*Stress (all maker):* (a) `--windows 3` 120d → **NOT DURABLE**, but only because the **oldest** 120d
slice (240–360d ago) returns `—` (no confirmable edge / too few qualifying extremes in that narrow
slice); the trailing two windows still confirm (+5.3 / +8.2). (b) `--windows 2` **180d** → NOT DURABLE
but the harness fires its **sign-stable NOTE**: full +5.6 / +14.0bps (both positive, *no flip*), blocked
only by the trailing window's weak OOS tail (+1.4, sharpe < 1) — the "regime-sensitive lead, not
artifact" failure mode, the *same* one the B-horizon majors-momentum lead hit. (c) `--windows 2` 120d,
**2-pair** (ETH/BTC|SOL/AVAX only) → same: full +9.5 / +7.8 (sign-stable), trailing OOS weak (+2.0).

**Honest read.** Across **every** window length, window count, and basket tried, the full-sample maker
edge is **positive and sign-stable** — it *never* shows the cross-window sign-flip that hand-pruned the
six earlier theses. It is the **only** candidate to clear the canonical 120d×2-window durability bar
outright. That makes it a materially stronger lead than anything before it. It is **not** unconditionally
durable yet: it is **maker-only** (taker ≈ breakeven), and at longer windows / narrower pair sets it
degrades to the regime-sensitive within-window failure (sign-stable, not artifact). So: **a genuine lead
to push, not a deploy and not a prune.** No G0-PASS claim beyond the canonical bar it cleared; **maker-only,
nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **162 passed** (+7); `ruff check src tests scripts` → clean.
Committed increment is the agent + CLI registration + 7 offline tests; all confirm numbers are
measurement (`--no-cache`, no `data/` writes). No strategy in the live roster changed; no live mode
enabled.

**What's next (loop).** First real lead in the search — push it before anything else. Priority slices:
(2) **plateau sweep** lb / entry_z / exit_z at 1h (is the canonical-bar PASS a robust plateau or a knife
edge?); (3) **held-out pair set** (does the 2-window PASS survive disjoint liquid pairs not in the
default basket — the leave-pairs-out analogue of leave-one-coin-out?); (4) **longer per-window `--days`**
to test whether the trailing-window OOS-tail weakness at 180d is a boundary artifact (the test that
pruned B-horizon); (5) **intraday cadence (15m/5m)** where short-horizon spread reversal is strongest and
the maker edge per round-trip should be larger relative to the move (REVIEW C7). If it holds the
canonical bar across a sweep + held-out pairs, it becomes the first paper-deploy candidate behind the
existing 5×/1× risk machinery (human-gated).

---

## Iteration 30 — 2026-06-08 — B-pairs slice 2: plateau-sweep harness (PASS = robust plateau, not a knife-edge), as code — and run on the pairs lead

**Context.** Iteration 29 found the first signal ever to clear the canonical 120d×2-window durability
bar: `pairs_reversion_v1` at lb=48 / entry_z=2.0. The standing "what's next" was slice (2): **is that
PASS a robust plateau or a knife-edge?** A durability PASS at exactly one parameter value is almost
always curve-fit to that value. Rather than eyeball a one-off sweep, codify the question the same way
the out-of-time bar was codified into `confirm_across_windows` (B-mw): one repeatable rule every future
candidate runs.

**Code increment (committed, with tests).** New plateau-sweep machinery in `backtest/confirm.py`:
- `classify_plateau(points, min_plateau=2)` — pure: a robust optimum is a **contiguous run of
  ≥min_plateau adjacent passing values**; a single passing value flanked by failures is a knife-edge.
  Returns `(is_plateau, longest_run_values)`. Helper `_longest_true_run`.
- `sweep_param(param, values, evaluate, …)` — driver that calls a caller-supplied
  `evaluate(value)->(passing, full_edge_bps)` across a value grid and renders a PLATEAU / NO-PLATEAU
  verdict with reasons. Pure (no network/agent) so it's unit-tested directly.
- `SweepPoint` / `SweepResult` dataclasses with `summary()`.
- CLI: `hlbot confirm --sweep 'lookback_bars=24,36,48,72'` (respects `--windows`): loads history **once**
  and reuses it across all values, runs `confirm_strategy` (1 window) or `confirm_across_windows` (≥2,
  passing=durable), prints the plateau verdict, exits 1 if no plateau. Refactored the `confirm` factory
  build into a `_factory_for(config)` closure so a swept value substitutes cleanly into the agent config.
  New pure `_parse_sweep('param=v1,v2,…') -> (param, [values])` (≥2 values required, same int→float→bool→str
  typing as `--params`).
- Tests (+13): `tests/test_confirm_sweep.py` (contiguous run = plateau; isolated pass = knife-edge; two
  non-adjacent passes ≠ plateau; no pass; min_plateau=3; longest-run selection; summary render) and
  `tests/test_parse_sweep.py` (typed int/float values, whitespace, missing `=`, empty param, single-value
  rejected).

**Evidence — slice 2 run on the pairs lead (real HL history, network; 6 coins BTC,ETH,SOL,AVAX,LINK,AAVE,
1h, 120d, `--windows 2`, `--prefer maker`; "passing"=DURABLE; edge = trailing-window full-sample maker):**

*lookback_bars (coarse 24/36/48/72/96):* **NO PLATEAU** — only lb=48 durable (full +5.3); 36 +3.7 (not
durable), 24 +1.5, 72/96 `—` (trade-starved: longer lookback smooths the spread → too few |z|≥2 events).

*lookback_bars (fine 40/44/48/52/56/60):* **✅ PLATEAU** — lb ∈ {48, 52, 56} all DURABLE (+5.3 / +5.6 /
+4.0); 40 +0.8, 44 +1.9 (below the plateau's lower edge, not durable); 60 `—` (trade-starved). So the
lb PASS is a **narrow but real plateau (~48–56, width ~16 bars)**, bounded below by weakening edge and
above by trade-starvation — the coarse grid stepped over it because 72/96 starve and 24/36 sit below it.

*entry_z (1.5/1.75/2.0/2.25/2.5):* **NO PLATEAU (knife-edge)** — only entry_z=2.0 DURABLE (+5.3). The
full-sample maker edge is **positive and sign-stable across the whole range** (1.5 +2.5 → 1.75 +4.1 →
**2.0 +5.3** → 2.25 +2.8 → 2.5 +1.6, a clean peak at 2.0), so off-peak values are regime-sensitive, not
the artifact sign-flip — but the *durability* PASS hinges on entry_z=2.0 specifically.

**Honest read.** Slice 2 partially de-risks and partially fragilizes the Iter-29 lead. The lookback
choice sits on a genuine (if narrow) plateau — good, not a single-point fit. But the entry_z choice is a
**knife-edge under the strict 2-window bar**: the durability PASS depends on entry_z=2.0. The edge curve
in entry_z is smooth and peaked (not noisy), which argues the 2.0 optimum is structural rather than luck,
but it does mean the canonical-bar PASS is **less robust than a single number suggested** — it needs both
lb∈[48,56] AND entry_z≈2.0. Still a lead worth pushing (it remains the only candidate to clear the bar at
all, and now on a lookback plateau), but the entry_z knife-edge is the thing the next slices must watch.
**Maker-only, nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **175 passed** (+13); `ruff check src tests scripts` → clean.
All sweep numbers are measurement (no `data/` writes from the durability runs were committed). No strategy
in the live roster changed; no live mode enabled.

**What's next (loop).** The plateau harness is now reusable for every future candidate. Remaining B-pairs
slices, re-prioritized by what slice 2 surfaced: (3) **held-out pair set** (does the 2-window PASS survive
disjoint liquid pairs not in the default basket? — the leave-pairs-out analogue of leave-one-coin-out; the
most important next test now that we know the param region is narrow); (4) **longer per-window `--days`**
to test whether the 180d trailing-OOS-tail weakness is a boundary artifact; (5) **intraday cadence
(15m/5m)** where short-horizon spread reversal is strongest and more entry events should *widen* the lb
plateau and relieve the trade-starvation ceiling. If held-out pairs hold the bar on the lb plateau at
entry_z≈2.0, the lead graduates toward a paper-deploy candidate behind the existing 5×/1× risk machinery
(human-gated).

---

## Iteration 31 — 2026-06-08 — B-pairs slice 3: held-out pair set — THE LEAD DOES NOT GENERALIZE (basket-specific, sign-flips on disjoint liquid pairs)

**Context.** Iterations 29–30 established `pairs_reversion_v1` as the first and only signal to clear the
canonical 120d×2-window durability bar (DURABLE at lb=48 / entry_z=2.0, on a narrow but real lookback
plateau). The standing "what's next", made *most important* by the narrow param region slice 2 surfaced,
was slice (3): **does the 2-window PASS survive disjoint liquid pairs not in the default basket?** — the
leave-pairs-out analogue of the leave-one-coin-out test that has pruned earlier leads. If the edge is a
property of *pairs-reversion as a strategy class* it should survive a fresh, economically-sensible pair
set; if it's specific to the three pairs it was specified with, a disjoint set will fail.

**Code increment (committed, with tests).** The held-out pairs were otherwise a hand-typed
`--params 'pairs=ARB/OP|...'` string — exactly the typo/provenance risk B-baskets (Iter 27) fixed for the
coin universe, but pairs had no resolver. Added the pairs analogue in `backtest/baskets.py`:
- `PAIR_BASKETS` — named pair universes pinned to canonical `'A/B|C/D'` strings: `pairs_default`
  (ETH/BTC|SOL/AVAX|LINK/AAVE, the Iter-29 lead) and `pairs_heldout` (ARB/OP|APT/SUI|DOGE/WIF — two L2
  govs, two Move L1s, two memes; **no leg overlaps** pairs_default).
- `resolve_pairs(spec)` — expands basket names / passes bare `A/B` pairs through, upper-cases legs,
  dedupes by first occurrence, skips malformed/self pairs (the pairs analogue of `resolve_basket`; bare
  specs round-trip unchanged → backward compatible).
- `coins_in_pairs(spec)` — flat, order-preserving leg list, and `resolve_basket` now expands a pair-basket
  name to exactly those legs so `--coins pairs_heldout` can never drift from the pairs it trades.
- Wired into `confirm`/`backtest`: a string `pairs` param is run through `resolve_pairs` before the agent
  builds, so `--params 'pairs=pairs_heldout'` works. +8 unit tests (`tests/test_baskets.py`): basket
  expansion, bare round-trip, mix+dedupe, malformed/self skip, held-out⊥default disjointness, flat-leg
  order, `--coins` pair-basket expansion, canonical-value self-resolution.

**Evidence — slice 3 run (real HL history, network; 1h, 120d, `--windows 2`, `--prefer maker`, lb=48,
entry_z=2.0 — the exact Iter-29 config; full = trailing-window full-sample maker edge):**

*Default basket (sanity re-reproduce, `pairs_default`):* **✅ DURABLE** — trailing full **+5.3bps**
(in +6.0 / oos +3.3), older full +8.2 (in +7.1 / oos +10.7). Matches Iter-29 (+5.3 / +8.0); the new
wiring is sound and the lead is real *on its basket*.

*Held-out basket (`pairs_heldout` = ARB/OP|APT/SUI|DOGE/WIF):* **❌ NOT DURABLE — and it FLIPS SIGN.**
trailing full **−4.7bps** (in −3.2 / oos −8.3), older full +5.8 (in +2.7 / oos +14.9). The harness fires
its artifact verdict: *"full-sample edge FLIPS SIGN across windows (+5.8 … −4.7bps) — window-specific
artifact, not a durable edge."* This is the **same** failure signature that hand-pruned all six earlier
theses.

*Strong-pairs-only control (drop the weak meme leg → ARB/OP|APT/SUI):* **worse, still sign-flips.**
trailing full **−6.9bps** (in −6.6 / oos −6.8 — *cleanly* negative across the whole window), older +0.4.
So the held-out failure is **not** an artifact of one poorly-cointegrated meme pair (DOGE/WIF): the two
strongly-related, liquid held-out pairs are net-negative on the trailing window by themselves.

**Honest read — this is a major fragilization of the lead, not a confirmation.** Pairs-reversion's edge
**does not generalize** to a disjoint liquid pair set; it is **basket-specific**. The +5.3bps DURABLE
result lives in the three default pairs (most plausibly ETH/BTC, by far the strongest cointegration of the
three), not in "pairs reversion" as a tradeable strategy class. A fresh, economically-sensible pair set
produces the exact cross-window sign-flip the durability bar exists to reject. The lead is **not fully
pruned** — the default basket genuinely clears the bar and the lookback plateau, and ETH/BTC is a
legitimately strong relationship — but the *strategy-class generalization claim is dead*: the only
candidate ever to clear the canonical bar clears it **only on the basket it was specified with**. That is
the textbook signature of basket selection (hindsight pair choice), and it means this is not yet a
deployable book. **Maker-only, nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **183 passed** (+8); `ruff check src tests scripts` → clean.
All confirm numbers are measurement (`--no-cache`, no `data/` writes committed). No strategy in the live
roster changed; no live mode enabled.

**What's next (loop).** Slice 3 reorders the remaining work. The decisive question is now slice (4):
**leave-one-pair-out *within* the default basket** — run ETH/BTC, SOL/AVAX, LINK/AAVE each *alone* and the
three leave-one-out triples through the same bar. If the +5.3 collapses to ETH/BTC, the "lead" is a
single-relationship bet (one cointegrated pair), not a strategy — informative, but not a deployable book
on its own. If two+ pairs each carry independently, the basket-specificity is milder and the lead partly
survives. Either way, **slice (4) gates any paper-deploy talk** — a one-pair edge cannot justify the 5×/1×
machinery. Lower priority after that: (5) longer per-window `--days` (boundary-artifact check); (6)
intraday cadence (15m/5m). The pairs thesis is now a *narrow, possibly single-pair* lead, no longer "the
first durable signal" without the basket caveat.

---

## Iteration 32 — 2026-06-08 — B-pairs slice 4: leave-one-pair-out within the default basket — DURABILITY IS A 3-PAIR COMBINATION EFFECT, breaks under any leave-one-out (not a one-pair bet, but a single-basket knife-edge)

**Context.** Slice 3 (Iter 31) showed the only signal ever to clear the canonical 120d×2-window durability
bar — `pairs_reversion_v1` on the default basket (ETH/BTC|SOL/AVAX|LINK/AAVE, lb=48, entry_z=2.0, maker) —
**does not generalize**: a disjoint liquid pair set sign-flips. That made slice 4 the decisive gating
question for any paper-deploy talk: **is the +5.3bps DURABLE just ETH/BTC (the strongest cointegration),
or do multiple pairs carry it?** A one-relationship bet is not a deployable book.

**Code increment (committed, with tests).** Made leave-one-pair-out a one-command, reproducible probe
(mirrors `--windows`/`--sweep`):
- New pure `leave_one_pair_out(spec)` in `backtest/baskets.py` → for a multi-pair spec, the
  `(dropped_pair, remaining_spec)` for each pair (canonical, resolved/deduped first; `[]` for <2 pairs).
  The pairs analogue of leave-one-coin-out. +3 unit tests (`tests/test_baskets.py`): drops each pair with
  the correct 2-pair complement, resolves+dedupes bare input, needs ≥2 pairs.
- New `confirm --leave-one-out` flag (pairs agents, `--windows>=2`): loads history **once**, then runs the
  durability bar on the **full basket**, **each single pair alone**, and **each leave-one-pair-out subset**,
  printing a per-variant DURABLE/NOT-DURABLE verdict + each window's full-sample edge. Reuses the existing
  `_window_specs`/`_load`/`confirm_across_windows` plumbing; exits 1 only if no variant is durable.

**Evidence — slice 4 run (real HL history, network; ETH,BTC,SOL,AVAX,LINK,AAVE, 1h, 120d, `--windows 2`,
`--prefer maker`, lb=48, entry_z=2.0 — the exact Iter-29 config; full = [trailing, older] window
full-sample maker edge bps):**

```
✅ DURABLE      full: ETH/BTC|SOL/AVAX|LINK/AAVE   full[+5.3  +8.2]
❌ NOT DURABLE  only ETH/BTC                       full[+3.0  +8.7]   (both +, sign-stable; fails walk-fwd)
❌ NOT DURABLE  only SOL/AVAX                      full[+15.3 +6.9]   (both +, strongest single; fails walk-fwd)
❌ NOT DURABLE  only LINK/AAVE                     full[-8.8  +11.6]  (SIGN-FLIPS — the artifact signature)
❌ NOT DURABLE  drop ETH/BTC  (SOL/AVAX|LINK/AAVE) full[+4.8  +9.5]   (both +, sign-stable; fails walk-fwd)
❌ NOT DURABLE  drop SOL/AVAX (ETH/BTC|LINK/AAVE)  full[-2.6  +10.3]  (SIGN-FLIPS)
❌ NOT DURABLE  drop LINK/AAVE(ETH/BTC|SOL/AVAX)   full[+9.5  +7.8]   (both +, sign-stable; fails walk-fwd)
```

**Honest read — the durability is an emergent property of the *specific 3-pair combination*, and it is
not robust to leave-one-out.** Two results matter:
1. **It is NOT "just ETH/BTC."** ETH/BTC alone is the *weakest* durable-relevant single (+3.0 trailing,
   not durable); SOL/AVAX alone is the strongest (+15.3/+6.9). So the lead is not a single-relationship
   bet — that hypothesis is **disproved**.
2. **But NO subset clears the bar — only the exact 3-pair basket does.** Every single pair and every
   leave-one-out triple is NOT DURABLE; two of them (LINK/AAVE alone, and the ETH/BTC|LINK/AAVE pair-out)
   even **sign-flip** (the artifact signature). The DURABLE verdict survives **only** when all three pairs
   are pooled together. This is the textbook signature of a **portfolio/averaging effect**: pooling three
   imperfectly-correlated spreads smooths the walk-forward just enough to pass, even though no constituent
   passes on its own. Remove any one pair and the smoothing is gone.

**Net.** Slice 4 *replaces* "is it one pair?" with a sharper, worse answer: the canonical-bar PASS is a
**single-basket knife-edge**. The lead now clears the bar only under the full conjunction —
**lb∈[48,56] (slice-2 plateau) AND entry_z≈2.0 (slice-2 knife-edge) AND exactly these 3 pairs, all
required (slice 4) — and it fails on disjoint liquid pairs (slice 3).** That is a heavily over-conditioned
single point in (param × basket) space, with the durability resting on a 3-spread averaging effect rather
than any robust per-pair edge. **It is not a deployable book.** The `pairs_reversion_v1` agent is real,
well-tested, and stays in the roster for paper/measurement, but the "first signal to clear the durability
bar" claim must now carry the irreducible caveat that the PASS is **specific to the exact default basket
and parameter point** and does not survive either leave-pairs-out (slice 3) or leave-one-pair-out (slice
4). Maker-only, nothing touches capital, no live change.

**Evidence (gate).** `uv run pytest -q` → **186 passed** (+3); `ruff check src tests scripts` → clean.
All confirm numbers are measurement (cached candles, no `data/` writes committed). No strategy in the live
roster changed; no live mode enabled.

**What's next (loop).** Slice 4 effectively closes the durability-robustness investigation of the pairs
lead: it is characterized as a fragile, basket-and-param-specific PASS, not a generalizable edge. The seven
structurally-different theses are now all either pruned or (pairs) reduced to a single over-conditioned
point. Remaining lower-value pairs slices — (5) longer per-window `--days` (boundary-artifact check) and
(6) intraday 15m/5m cadence — would at best widen the param plateau, not fix the basket-specificity that
slices 3+4 established, so they are **deprioritized below a fresh structurally-different thesis**. The
highest-leverage next move is a new orthogonal edge hypothesis (or to reframe pairs-reversion as an
explicitly multi-pair *diversified* book and test whether a *larger, pre-committed* pair set is durable —
the inverse of leave-one-out — rather than chasing the 3-pair point). Pairs stays maker-only paper.

---

## Iteration 33 — 2026-06-08 — B-pairs slice 7: the "larger pre-committed diversified book" reframe — THE LAST RESCUE ANGLE FAILS (more diversification does NOT buy durability; pairs fully pruned)

**Context.** Slices 3–4 (Iter 31–32) closed the durability-*robustness* investigation of the only signal
ever to clear the canonical 120d×2-window bar — `pairs_reversion_v1` on the 3-pair default basket
(ETH/BTC|SOL/AVAX|LINK/AAVE, lb=48, entry_z=2.0, maker): it does **not** survive leave-pairs-out (a disjoint
liquid set sign-flips) **nor** leave-one-pair-out (no single pair / no triple subset is durable; the PASS is
a 3-spread *portfolio/averaging* effect). That left exactly one un-tested rescue, named explicitly in the
Iter-32 "what's next": **reframe pairs as an explicitly *diversified, pre-committed larger* pair book — the
inverse of leave-one-out.** If slice 4's averaging is the mechanism, pooling *more* imperfectly-correlated
spreads should make the book *more* durable, not less. This iteration tests that directly and decisively.

**Code increment (committed, with tests).** Added a single pre-committed pair basket and pinned it so the
result cites an auditable, hindsight-free universe:
- `pairs_diversified` in `PAIR_BASKETS` = **the exact union of `pairs_default` ∪ `pairs_heldout`**
  (`ETH/BTC|SOL/AVAX|LINK/AAVE|ARB/OP|APT/SUI|DOGE/WIF`) — 6 pairs, 12 distinct legs, six economic buckets
  (cross-cap majors, L1 alts, DeFi, L2 govs, Move L1s, memes). Crucially it makes **ZERO new pair choices**:
  both halves were already version-pinned in prior iterations, so this is the maximally-defensible inverse of
  leave-one-out (pooling, not selecting). +2 unit tests (`tests/test_baskets.py`): it resolves to exactly
  `resolve_pairs("pairs_default|pairs_heldout")`, and its 12 legs are all distinct (no leg reuse).

**Evidence — slice 7 run (real HL history, network; the 12 legs, 1h, 120d, `--windows 2`, `--prefer maker`,
lb=48, entry_z=2.0 — the exact Iter-29 config; full = window full-sample maker edge bps):**

```
❌ NOT DURABLE  pairs_reversion_v1  (2 windows, prefer=maker)
  ❌ trailing 120d          full  -0.1bps   in -0.7  oos -1.4
  ❌ 120d ending 120d ago   full  +3.4bps   in -0.1  oos +11.4
  - full-sample edge FLIPS SIGN across windows (+3.4 … -0.1bps) — window-specific artifact, not a durable edge
```

**Honest read — the portfolio/averaging rescue is disproved; pairs is now fully pruned as a deployable
edge.** Pooling *more* imperfectly-correlated spreads did **not** increase durability — it *reduced* it.
The held-out half (ARB/OP|APT/SUI|DOGE/WIF, net-negative on the trailing window per slice 3) drags the
6-pair pool's trailing full-sample edge down to ~zero/negative (−0.1bps, vs the 3-pair basket's +5.3), and
the book **sign-flips across windows** (+3.4 → −0.1) — the exact artifact signature the bar exists to reject.
So slice 4's smoothing was **specific to the three default pairs**, not a general "more diversification →
more durable" property: averaging only helps when the pooled constituents are individually edge-bearing in
the same direction, which the held-out pairs are not. This was the last structurally-distinct way the pairs
lead could have become a deployable book, and it fails.

**Net — the pairs investigation is CLOSED.** The only PASS that ever cleared the canonical durability bar
(the 3-pair +5.3bps DURABLE) is a heavily over-conditioned single point in (param × basket) space:
**lb∈[48,56] (slice-2 plateau) AND entry_z≈2.0 (slice-2 knife-edge) AND exactly the 3 default pairs, all
required (slice 4)** — and it fails on disjoint liquid pairs (slice 3, sign-flip), on every leave-one-pair-out
subset (slice 4), AND now on a pre-committed larger diversified book (slice 7, sign-flip). The remaining
deferred slices (5) longer per-window `--days` and (6) intraday 15m/5m are **moot**: each could only widen the
parameter plateau, neither can fix the basket-specificity that slices 3/4/7 jointly established. Seven
structurally-different theses (FEMR/carry, cross-sectional momentum ±regime, time-series momentum,
majors-1d momentum, and pairs-reversion) are now all pruned or reduced to an over-conditioned point after the
out-of-time durability bar. `pairs_reversion_v1` is real, well-tested, and stays in the roster for
paper/measurement only. **Maker-only, nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **188 passed** (+2); `ruff check src tests scripts` → clean. The
confirm numbers are measurement (cached candles, no `data/` writes committed). No strategy in the live roster
changed; no live mode enabled.

**What's next (loop).** With the pairs lead closed, the highest-leverage move is no longer another pairs
slice but a **fresh, structurally-different edge hypothesis** that the existing durability machinery
(`confirm --windows 2+`, `--sweep`, `--leave-one-out`, named baskets) can adjudicate cheaply. The seven
pruned theses share a failure mode worth learning from: each is a *price/funding-derived directional or
relative signal* that proves window-specific under walk-forward. Candidate orthogonal directions that do
**not** key off recent price/funding the same way: (a) a **cross-venue / micro-structure** signal (e.g.
maker-rebate capture conditioned on realized spread vs queue position — an *execution* edge, not a
*direction* edge, which is where REVIEW C1 says the structural money is); (b) a **calendar/funding-settlement
timing** effect (deterministic funding accrual windows, not funding *level*); (c) **basis/term-structure**
between perp and any available longer-horizon reference. The next iteration should pick one, specify it as a
new agent, and run it through `confirm --windows 2` from the first run (never a single trailing window).

## Iteration 34 — 2026-06-08 — B-session slice 1: session-timing — the EIGHTH structurally-different thesis (first that keys off NEITHER price NOR funding); sign-stable, mirror-consistent clock effect, but NOT durable (regime-sensitive lead)

**Context.** Iter 33 closed the pairs investigation (all seven price/funding-derived theses now pruned or
reduced to an over-conditioned point) and named three candidate orthogonal directions for the next thesis:
(a) execution/maker-rebate, (b) **calendar/funding-settlement timing**, (c) basis/term-structure. This
iteration picks (b) — the cheapest to adjudicate with the existing durability machinery and the only class
that keys off **clock time, not recent prices or funding levels** — and runs it through the out-of-time bar
from the first run.

**Code increment (committed, with tests).** New `session_timing_v1` agent (`agents/session_timing.py`),
registered in both `confirm`/`backtest` factory maps:
- **Thesis (a priori, not data-mined):** liquid majors inherit TradFi equity beta, so they realize a
  different average drift *during* the US equity cash session (~13:30–20:00 UTC, weekdays) than *overnight /
  on weekends*. The agent takes net-directional LONG exposure **only inside an a-priori-fixed UTC hour band**
  (default 14–21Z, weekdays) and is flat outside; the window is specified in advance from the TradFi
  correlation (no hour search), which keeps it out of the data-mining trap the momentum leads fell into.
- **Pure, unit-testable core:** `in_session(ts_ms, enter_hour_utc, exit_hour_utc, weekdays_only, invert)`
  reads **zero** price/funding input — only the bar's UTC hour + weekday from `view.ts_ms`. An `invert` flag
  trades the exact complement (overnight/weekend) at no extra code, so both halves of the clock are testable.
- 8 unit tests (`tests/test_session_timing.py`): window-edge inclusivity (lower inclusive / upper
  exclusive), weekend exclusion under `weekdays_only`, `invert` is the exact complement over all 24 hours,
  midnight-wrapping window, and the decide() path (enters the eligible liquid universe long in-session,
  filters illiquid coins, holds flat outside, flattens held positions when the session closes).

**Evidence — durability bar (real HL history, network; majors = BTC,ETH,SOL,HYPE, 1h, 120d, `--windows 2`,
`--prefer maker`; default 14–21Z weekday window; full = [trailing, older] window full-sample maker edge bps):**

```
BASE (long US session 14-21Z, weekdays):
❌ NOT DURABLE  session_timing_v1  (2 windows, prefer=maker)
  ❌ trailing 120d          full  +11.4bps   in +17.0  oos  -1.3
  ❌ 120d ending 120d ago   full   +0.4bps   in  -3.2  oos  +8.9
  - NOTE: full-sample edge POSITIVE & SIGN-STABLE across both windows — blocked only by within-window
    walk-forward (regime-sensitive lead, NOT the cross-window artifact sign-flip)

INVERT (long overnight/weekend — the complement):
❌ NOT DURABLE  session_timing_v1  (2 windows, prefer=maker)
  ❌ trailing 120d          full   -6.3bps   in  -0.5  oos -17.9
  ❌ 120d ending 120d ago   full  -26.6bps   in -10.9  oos -62.4
```

**Honest read — a genuinely coherent, structurally-new signal, but still not durable.** Two things make this
the *most coherent* first-run result of any thesis so far, and one thing keeps it a lead rather than a deploy:
1. **The base long-US-session edge is positive and SIGN-STABLE across both disjoint 120d windows** (+11.4 /
   +0.4 maker), tripping the harness "lead, not artifact" NOTE — it is *not* the cross-window sign-flip that
   hand-pruned the six earlier theses.
2. **The mirror is clean and directionally consistent:** the complement (long overnight/weekend) is
   **negative in BOTH windows** (−6.3 / −26.6, no sign flip). So across two independent 120d windows majors
   drift *up* in the US session and *down/flat* overnight — a coherent, repeatable clock effect, not a single
   window's noise. This is exactly the cross-window consistency the seven price/funding theses lacked.
3. **But the base case still fails the within-window walk-forward:** the trailing window's edge lives in its
   in-sample half (+17.0) and evaporates OOS (−1.3); the older window inverts (in −3.2 / oos +8.9). The
   in/oos relationship is itself regime-dependent, so walk-forward correctly rejects. Same failure *mode* as
   the majors-1d momentum lead (Iter 25–27): sign-stable, cost-surviving, but regime-sensitive within each
   window — a lead to push, not a deployable edge.

**Net.** Session-timing is the **eighth** structurally-different thesis and the first to key off neither
price nor funding. It produces a **sign-stable, mirror-consistent** session effect on majors (long-session
positive, overnight negative, both across two disjoint windows) — stronger cross-window coherence than any
pruned thesis — yet still **does not clear the durability bar** because the within-window walk-forward is
regime-sensitive. It joins the "sign-stable lead, not deployable" bucket alongside majors-1d momentum, NOT
the artifact-sign-flip prune bucket. `session_timing_v1` is real, well-tested, and stays in the roster for
paper/measurement only. **Maker-only, nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **196 passed** (+8 incl. shared); `ruff check src tests scripts`
→ clean. The confirm numbers are measurement (cached candles, no `data/` writes committed). No strategy in
the live roster changed; no live mode enabled.

**What's next (loop).** Session-timing is a *lead* (sign-stable + mirror-coherent), so unlike the prune
bucket it is worth one or two cheap push-slices before a verdict — and they are the natural inverse of what
broke the momentum leads: (1) **wider/longer windows** — `--windows 3` and longer per-window `--days` to see
whether the within-window walk-forward failure is a boundary artifact or persists across ~2yr; (2) **basket
breadth** — run on a wider majors set and on liquid alts to test whether the session effect is a
majors/equity-beta property or generalizes; (3) **window plateau** — `--sweep` the enter/exit hours around
the a-priori US-session band to confirm a contiguous plateau (a real session effect should not be a
single-hour knife-edge). If the trailing OOS tail and the older-window in<oos inversion both survive (1)+(2),
this becomes the strongest durability candidate yet; if they don't, it prunes cleanly as the eighth
regime-sensitive lead. Either way it is adjudicated by the existing `confirm --windows`/`--sweep` machinery.

## Iteration 35 — 2026-06-08 — B-session slices 2–4: push the session-timing lead to a verdict — PRUNED as a deployable edge (strongest-characterized lead yet, but the within-window regime-sensitivity is NOT a boundary artifact and it sign-flips on alts)

**Context.** Iter 34 introduced `session_timing_v1` (the eighth thesis, first keying off neither price nor
funding) and found a genuine *lead*: a **sign-stable, mirror-coherent** US-session drift on majors (long-session
positive +11.4/+0.4 across two disjoint 120d windows, the overnight complement negative in both), but NOT
DURABLE because the within-window walk-forward is regime-sensitive. The backlog named three cheap push-slices
to reach a verdict. This iteration runs all three with the existing durability machinery (`confirm
--windows/--days/--coins/--sweep`) — no code change, pure measurement — and reaches a clean prune/advance call.

**Evidence — all maker, real HL history, `full` = preferred-execution full-sample edge bps per window:**

```
(3) BREADTH — wider majors (majors_wide = 12 coins, 1h, 120d, --windows 2):
❌ NOT DURABLE  session_timing_v1   — SIGN-STABLE (NOTE fires)
  ❌ trailing 120d          full  +11.4bps   in +17.0  oos  -1.3
  ❌ 120d ending 120d ago   full   +0.6bps   in  -3.0  oos  +9.1
  -> widening majors does NOT break sign-stability (the momentum lead broke here);
     equity-beta hypothesis survives breadth on majors.

(3) BREADTH — liquid alts (alts_heldout, 1h, 120d, --windows 2):
❌ NOT DURABLE  session_timing_v1   — SIGN-FLIPS (artifact signature)
  ✅ trailing 120d          full  +24.4bps   in +22.1  oos +29.6   (even CONFIRMS)
  ❌ 120d ending 120d ago   full   -1.7bps   in  +4.7  oos -17.4
  -> the effect does NOT generalize to alts; the trailing alt strength is window-specific.

(2) LONGER BASELINE — 1h, --windows 3, 120d each (majors):
❌ NOT DURABLE  session_timing_v1
  ❌ trailing 120d          full  +11.4bps   in +17.0  oos  -1.3
  ❌ 120d ending 120d ago   full   +0.4bps   in  -3.2  oos  +8.9
  ❌ 120d ending 240d ago   full        —    in   —    oos   —
  -> oldest window data-limited (no trades; HL 1h candle history ~208d cap) — the
     1h baseline can't extend past ~240d. Drove the 4h longer-baseline below.

(2) LONGER BASELINE — 4h, 240d each, --windows 2 (majors; ~480d / ~1.3yr total):
❌ NOT DURABLE  session_timing_v1   — SIGN-STABLE (NOTE fires)
  ❌ trailing 240d          full   +3.2bps   in +10.7  oos -13.8
  ❌ 240d ending 240d ago   full   +2.7bps   in  +4.1  oos  -0.6
  -> DEFINITIVE: full-sample edge stays positive & sign-stable at a 2× baseline, but the
     OOS tail is negative in BOTH windows — the within-window regime-sensitivity is NOT a
     boundary/horizon artifact; it persists with more data.

(4) HOUR-BAND SWEEP — enter_hour_utc in {12,13,14,15,16} (exit 21Z fixed), 1h, 120d, --windows 2 (majors):
❌ NO PLATEAU (knife-edge by the binary durability criterion — no value clears full durability,
   expected for a regime-sensitive lead), BUT the full-sample edge is a SMOOTH HILL peaked at the
   a-priori open:
     12 -> +4.4 | 13 -> +9.5 | 14 -> +11.4 (peak) | 15 -> +8.4 | 16 -> +5.0
  -> NOT a single-hour knife-edge / NOT data-mined to one lucky hour; the effect is a broad,
     smoothly-located session window cleanly peaked exactly where the TradFi hypothesis predicts.
```

**Honest read — the strongest lead in the search, and still not durable.** Session-timing accumulates more
robustness evidence than any prior thesis: it is **sign-stable on majors across two windows AND a 2× longer
~480d baseline**, **mirror-coherent** (slice 1: the overnight complement is cleanly negative), **hour-robust**
(slice 4: a smooth hill peaked at the a-priori 14Z open, not a knife-edge), and **breadth-robust on majors**
(slice 3: widening to 12 coins keeps sign-stability, whereas breadth *broke* the majors-1d momentum lead). It
is genuinely a coherent, repeatable equity-beta session effect on majors. **But it does not clear the
durability bar, for two now-conclusive reasons:** (1) the within-window walk-forward OOS tail is negative
regardless of horizon — the 4h ~480d baseline shows the regime-sensitivity is **not a boundary artifact**, it
persists with 2× the data (the one hypothesis that could have rescued the lead); and (2) it **sign-flips on a
disjoint liquid-alt basket** (alts trailing window even confirms, but the older alt window is the artifact
sign-flip) — so the "durable" behavior is specific to the majors universe and a favorable trailing regime.

**Net.** Session-timing is the eighth structurally-different thesis, now **fully characterized and pruned as a
deployable edge.** It is the *best* lead the search has produced — and that is exactly why running it to a
verdict is valuable: it shows that even a sign-stable, mirror-coherent, hour-robust, majors-breadth-robust
clock effect fails the durability bar through the same regime-sensitivity that killed the seven price/funding
theses, now proven (via the 2× baseline) not to be a horizon artifact. It joins the "sign-stable lead, not
deployable" bucket alongside majors-1d momentum. `session_timing_v1` is real, well-tested (8 unit tests), and
stays in the roster for paper/measurement only. **Maker-only, nothing touches capital, no live change.**

**Evidence (gate).** `uv run pytest -q` → **196 passed** (no code change this iteration; pure measurement on
cached candles, no `data/` writes committed); `ruff check src tests scripts` → clean. No strategy in the live
roster changed; no live mode enabled.

**What's next (loop).** Eight direction/relative theses are now pruned (or reduced to an over-conditioned
point), all sharing one failure mode: a price/funding/clock-derived *directional* signal that proves
regime-sensitive under walk-forward. The unexplored class — and the one REVIEW C1 says holds the structural
money — is an **execution edge, not a direction edge**: capturing the realized spread / maker rebate itself,
net of adverse selection, which is not a directional bet and so may sidestep the regime-sensitivity. Filed as
**B-exec** (`maker_spread_v1`): the next iteration should specify the agent + a backtest-able
fill/adverse-selection model and run it through `confirm --windows 2 --prefer maker` from the first run. The
one remaining session angle (finer time-of-day resolution, B-session-tod) is parked low — the smooth hour-hill
(slice 4) and the 2× baseline OOS failure (slice 2) make it unlikely to fix durability.

## Iteration 36 — 2026-06-08 — B-exec slice 1: maker-spread capture model (the NINTH thesis, first *execution* edge not *direction* edge) — naive symmetric quote is net-NEGATIVE, sign-stable across both windows AND 1h/5m cadence (adverse selection > captured spread)

**Context.** Eight direction/relative theses are pruned or reduced to an over-conditioned point, all sharing
one failure mode: a price/funding/clock-derived *directional* signal that proves regime-sensitive under the
walk-forward bar. REVIEW C1 says the structural money is in **execution, not direction** (the taker tax is
~73% of the historical bleed; B1). B1 measured that *maker execution of a directional signal* doesn't create
edge, but it never tested capturing the **spread/rebate itself** as the edge — a passive two-sided maker that
earns the realized half-spread + rebate net of adverse selection (fill-when-wrong). That is **not a
directional bet**, so it may sidestep the regime-sensitivity that killed the eight directional theses. This
iteration specifies the model and runs it to a first verdict.

**Why a dedicated model, not the engine path.** The replay engine fills every `place`/`flatten`
deterministically at the bar mid (maker mode = mid, zero slippage). That can represent a *directional* maker
bet but **cannot represent spread capture or adverse selection**: it never fills *at* a resting limit and
never conditions the fill on price trading into the quote. So slice 1 is a pure, no-lookahead intrabar fill
simulator over real OHLC candles.

**What changed (code).** New `src/hl_bot/backtest/maker_spread.py` (pure, unit-tested): rests a passive
two-sided quote each bar — bid at `m0*(1−hs)`, ask at `m0*(1+hs)`, anchored to the *prior* bar's mid `m0`
(no lookahead). Bid fills iff this bar's low ≤ bid; ask fills iff this bar's high ≥ ask. A filled lot is
marked to this bar's close `mi`, so each fill decomposes exactly into **captured half-spread** (`m0`−fill ≈ hs)
**minus adverse drift** (the mid moved past the fill) **minus maker fee + rebate** — adverse selection emerges
from the realized price path, not an assumption. `simulate_maker_spread` (single coin) / `simulate_universe`
(pool fills equal-notional) / `bars_from_candles` (raw HL candle adapter) + `MakerSpreadResult` with the
decomposition. 10 unit tests pin the fill rule, the spread/adverse/fee identity, rebate, and the
spread↔fill-rate tradeoff.

**Evidence — real HL history, majors (BTC,ETH,SOL,HYPE,AVAX,LINK), maker_fee=1.0bp, no rebate. `net` =
per-fill net-of-cost edge (bps):**

```
1h, two disjoint 120d windows (trailing + 120d-older):
  half_spread  trailing net        older net          (gross − adverse − fee)
    2bps        -3.14   (fr 93.5%)   -3.26  (fr 93.8%)   2.00 − ~4.2 − 1.0
    5bps        -3.05   (fr 88.6%)   -3.31  (fr 89.0%)   5.00 − ~7.2 − 1.0
   10bps        -2.71   (fr 80.6%)   -3.01  (fr 81.4%)  10.00 − ~11.9 − 1.0
   20bps        -2.59   (fr 65.1%)   -2.75  (fr 67.2%)  20.00 − ~21.7 − 1.0
  -> NET-NEGATIVE at every half-spread, BOTH windows, SIGN-STABLE. Adverse selection
     runs ~1.5–2bps ABOVE the captured spread at every width; widening the quote raises
     gross but adverse rises in lockstep (you only fill when price trades into you), so
     net stays ~−2.6 to −3.3bps/fill. Fees finish it.

5m, trailing 20d (history cap), same basket:
    2bps  -3.41 (fr 78.5%) | 5bps -3.46 (fr 64.6%) | 10bps -3.48 (fr 46.7%)
  -> finer cadence does NOT rescue it: adverse still ~2.4–2.5bps above gross. Marking a
     fill to the *same bar's close* still attributes the intrabar continuation as adverse.
```

**Honest read.** The naive **symmetric** two-sided maker quote, marked bar-to-close, has **no net-of-cost
execution edge** on liquid majors: realized adverse selection (the directional drift *conditional on a fill*
— a bid fills precisely on down-bars whose close is below the prior mid by more than the half-spread) exceeds
the quoted half-spread at every width and at both 1h and 5m cadence, and the result is sign-stable across two
disjoint 120d windows (not the artifact sign-flip). This is the cleanest sign-stable result of any thesis —
because it isn't a directional bet, there is no regime to flip — but it's sign-stably *negative*. **One
structural sub-signal survives in the model and is the only live rescue angle:** bars where **both** sides
fill (`n_both`) are adverse-free round-trips (you end flat, earning ~2×spread − 2×fee); the bleed is entirely
from **single-sided fills that carry inventory into the adverse move.** A maker that only round-trips (or
skews/cancels to avoid one-sided inventory) is the untested variant.

**Net.** B-exec slice 1 delivers the model + a robust first verdict: passive symmetric spread capture on
bar-marked majors is net-negative and sign-stable. Not yet a full prune of the execution thesis — two
unrun angles remain: (a) **round-trip-only / inventory-skew** quoting (isolate the adverse-free `n_both`
fills, the only positive structure the model found), and (b) **tick/touch-level marking** (1h/5m close-marking
is a pessimistic adverse proxy; a real maker often recaptures the spread within seconds — the bar mark
over-attributes drift as adverse). Both are model refinements, not new directional theses. Maker-only,
nothing touches capital, no live change.

**Evidence (gate).** `uv run pytest -q` → **206 passed** (+10 maker-spread); `ruff check src tests scripts`
→ clean. The edge numbers are measurement (live candle fetch, no `data/` writes committed). No strategy in
the live roster changed; no live mode enabled.

**What's next (loop).** Push B-exec one slice further before any verdict, since it found real positive
structure (`n_both` round-trips) the eight directional theses never had: (1) add a **round-trip / inventory-skew**
mode to the model (only book the adverse-free both-sides fills, or cancel the resting side once inventory is
held) and run it through the two-window bar — the natural test of whether the execution edge lives in
disciplined round-tripping; (2) if that also fails, the symmetric+skew execution thesis prunes cleanly as the
ninth. File the round-trip slice as **B-exec-roundtrip**.

## Iteration 37 — 2026-06-08 — B-exec-roundtrip (slice 2): inventory-skew / round-trip-only maker — the adverse-free `n_both` rescue FAILS; net-NEGATIVE & sign-stable at every half-spread & both windows → the NINTH (execution) thesis is PRUNED

**Context.** Slice 1 (Iter 36) built the maker-spread model and found the naive **symmetric** two-sided
quote net-negative & sign-stable on majors (−2.6 to −3.3bps/fill, both 120d windows, all half-spreads): adverse
selection runs ~1.5–2bps above the captured half-spread because a resting bid fills precisely on down-bars.
The ONE positive structure the model found was the **both-sides-fill bar** (`n_both`): an adverse-free
in-bar round-trip (~2×spread − 2×fee, end flat) — the entire bleed comes from **single-sided fills carrying
inventory into the adverse move.** Slice 2 tests the obvious rescue: a maker that *only* round-trips.

**What changed (code, with tests).** Added an inventory-skew variant to `backtest/maker_spread.py`
(`simulate_maker_inventory` / `simulate_universe_inventory` + `MakerInventoryResult`, no-lookahead, pure).
It holds **at most one lot** and **skews fully against inventory**: while flat it quotes both sides at
`m0*(1∓hs)`; a both-sides-fill bar books an adverse-free in-bar round-trip; a single-sided fill leaves a lot
and from the next bar the maker quotes **only the reducing (exit) side** until it fills, then realizes the
round-trip and resumes two-sided quoting. Realized PnL decomposes per completed round-trip into captured
spread (≈2 half-spreads) − mid drift over the hold (adverse) − 2 maker fees; lots still open at series end are
reported (`unclosed_inventory`) and **not** booked (no lookahead, no optimistic mark). +6 unit tests pin the
in-bar adverse-free identity, the carried-round-trip hold-drift decomposition, the ≤1-lot/no-double-entry
skew, unclosed handling, and universe pooling.

**Evidence — real HL history, majors (BTC,ETH,SOL,HYPE,AVAX,LINK), 1h, maker_fee=1bp, no rebate, two disjoint
120d windows. SYM = slice-1 symmetric (net bps/fill); INV = inventory-skew (net bps/round-trip):**

```
                    SYM net/fill      INV net/round-trip      INV decomposition (pooled over ALL round-trips)
trailing 120d (17286 bars):
  hs= 2bps   -3.14 (fr 93.5%)   -6.25  per-quote -5.50   rt=15194 (inbar 13259 / carr 1935, hold 0.1)  gross 4.0 − adv 8.2
  hs= 5bps   -3.05 (fr 88.6%)   -6.11  per-quote -4.88   rt=13789 (inbar 10763 / carr 3026, hold 0.3)  gross 10.0 − adv 14.1
  hs=10bps   -2.71 (fr 80.6%)   -5.07  per-quote -3.48   rt=11882 (inbar  7689 / carr 4193, hold 0.5)  gross 20.0 − adv 23.1
  hs=20bps   -2.59 (fr 65.1%)   -4.89  per-quote -2.49   rt= 8783 (inbar  3724 / carr 5059, hold 0.9)  gross 40.0 − adv 42.9
older 120d (12736 bars):
  hs= 2bps   -3.26 (fr 93.8%)   -6.49  per-quote -5.75   rt=11273 (inbar  9923 / carr 1350, hold 0.1)  gross 4.0 − adv 8.5
  hs= 5bps   -3.31 (fr 89.0%)   -6.46  per-quote -5.23   rt=10300 (inbar  8182 / carr 2118, hold 0.2)  gross 10.0 − adv 14.5
  hs=10bps   -3.01 (fr 81.4%)   -5.95  per-quote -4.18   rt= 8940 (inbar  6015 / carr 2925, hold 0.4)  gross 20.0 − adv 23.9
  hs=20bps   -2.75 (fr 67.2%)   -5.23  per-quote -2.82   rt= 6869 (inbar  3318 / carr 3551, hold 0.8)  gross 40.0 − adv 43.2
```

**Honest read — the rescue fails, and the decomposition shows exactly why.** Disciplined round-tripping is
**net-negative at every half-spread, both windows, sign-stable** — and per-event *worse* than the symmetric
bleed (−4.9 to −6.5 bps/round-trip vs −2.6 to −3.3 bps/fill). The in-bar round-trips really are adverse-free
(`gross = 2×hs` exactly, adverse 0) and they dominate by **count** (~70–87% of round-trips), yet the pool
nets negative because **you cannot pre-select two-sided bars.** The ~13–37% of round-trips that are *carried*
(a single-sided fill you must unwind to stay ≤1 lot) inherit enormous realized adverse: e.g. at hs=2bps
trailing, pooled adverse 8.2 over 15194 round-trips with the 13259 in-bar ones contributing 0 ⇒ the 1935
carried round-trips average ~**64bps adverse each** (≈ −62bps net), and that tail alone sinks the whole book.
Skewing against inventory doesn't *avoid* adverse selection — it just **defers** it from "fill-when-wrong" to
"unwind-when-still-wrong." Widening the quote raises gross but adverse rises in lockstep (you only fill, and
only get to exit, when price has already moved), so net stays pinned negative. Same sign-stable signature as
slice 1: no direction = no regime to flip, but sign-stably *negative*.

**Net — the ninth, *execution* thesis is PRUNED.** Both forms of passive spread capture on bar-marked liquid
majors fail: the symmetric two-sided quote (slice 1) and the inventory-skew/round-trip-only variant (slice 2),
each net-negative and sign-stable across two disjoint 120d windows and every half-spread. The one positive
structure the model contained (adverse-free in-bar round-trips) is real but **unharvestable** — it cannot be
isolated without lookahead, and the single-sided inventory you're forced to carry to chase it is precisely the
slice-1 adverse bleed in deferred form. Nine structurally-different theses (eight directional + one execution)
are now pruned or reduced to an over-conditioned point. `maker_spread.py` (symmetric + inventory) stays as a
measurement/model tool; nothing touches capital, no live change.

**One model refinement remains parked (low priority), NOT a new thesis.** Both slices mark fills to the
**bar close**; a real maker often recaptures the spread within seconds, so 1h/5m close-marking is a
*pessimistic* adverse proxy that over-attributes intrabar continuation as adverse (B-exec-tickmark). The
inventory model partly answers this — its carried round-trips wait whole bars (avg hold up to 0.9 bars at
20bps), so much of their adverse is genuine multi-bar realized drift, not a single-bar marking artifact — but
a sub-bar (trade-tick) fill/mark study could still tighten the adverse estimate. It is parked below any fresh
thesis: the structural conclusion (passive symmetric/skew spread capture is net-negative on majors) is robust
to it, since even the *adverse-free* in-bar round-trips can't be harvested.

**Evidence (gate).** `uv run pytest -q` → **212 passed** (+6 inventory-skew); `ruff check src tests scripts`
→ clean. Edge numbers are measurement (live candle fetch, no `data/` writes committed). No strategy in the
live roster changed; no live mode enabled.

**What's next (loop).** Nine theses pruned across two orthogonal classes (direction and execution), all
net-negative or regime-sensitive after costs on this universe/cadence. The cheap, model-only angles are
exhausted; the remaining unexplored directions are structurally different *data/cadence* regimes rather than
new signals on 1h majors candles: (a) **sub-bar execution** — fetch trade-tick / L2 data and re-test maker
spread capture at the cadence the edge actually lives at (REVIEW C7: signal horizon ≫ 1h action cadence —
this is the honest version of the parked tick-mark refinement and the natural next *infrastructure* slice);
(b) **basis / term-structure** between perp and a longer-horizon reference (the one named candidate class from
Iter 33 never run); (c) accept the search's verdict and pivot to **honest measurement / paper track-record**
(Path C) so the negative-edge finding is itself the deliverable. The next iteration should pick one — (a) is
the highest-leverage because it directly attacks the cadence mismatch that REVIEW flagged as a root cause and
that every bar-marked backtest (incl. both maker slices) cannot rule out.

## Iteration 38 — 2026-06-08 — B-basis: perp-vs-spot basis reversion (TENTH thesis, the last named candidate class) — PRUNED; the one sign-stable-positive point is a knife-edge + per-coin-sign-flip averaging artifact

**Context.** Nine theses are pruned (eight directional + one execution), and Iter 37 flagged the
model-only candle search as exhausted with three named next directions: (a) sub-bar/tick execution, (b)
**basis / term-structure** (the one named candidate class never run, from Iter 33), (c) pivot to
measurement. (a) needs historical L2/trade-tick data HL doesn't serve deeply (blocked-ish); (b) turned out
**unblocked** — HL lists spot markets for the wrapped majors (UBTC `@142`, UETH `@151`, USOL `@156`) plus
native HYPE (`@107`), verified against `spotMeta`, so a same-venue perp-vs-spot basis `b = perp/spot − 1`
is directly measurable for the majors basket. Ran (b).

**Thesis (tenth, structurally-different).** The perp trades rich/cheap to its own spot between funding
stamps; cash-and-carry pressure pulls it back. Signal = rolling z-score of the basis: **SHORT the perp when
rich (z ≥ +entry), LONG when cheap (z ≤ −entry), exit on |z| ≤ exit.** It is *perp-only and directional*
(does not hold the spot leg — the engine trades perps only, so this is not a delta-neutral cash-and-carry
arb). Orthogonal to all nine pruned theses: keys off the **cross-market price gap of the same asset**, not a
coin's own return (momentum), funding *level* (carry), a *pairwise* ratio of two coins (pairs), the *clock*
(session), or *execution* microstructure. REVIEW M5's prior: majors basis is "tiny and well-arbitraged" — a
prior to test with a number, not assume.

**What changed (code, with tests).** New pure `src/hl_bot/backtest/basis_reversion.py` (mirrors the
self-contained `maker_spread.py` model, since the replay `Frame` has no spot reference and plumbing one in
for an unproven thesis is unjustified): `BasisBar` (perp+spot close, `.basis` property), `bars_from_candles`
(inner-join perp+spot raw candles by open time `t`), `SPOT_MARKETS` mapping, and no-lookahead
`simulate_basis_reversion` / `simulate_universe_basis` with a `BasisReversionResult` decomposing each
completed round-trip into the captured perp move (`direction × (perp_exit/perp_entry − 1)`) minus the
round-trip maker fee. The z-score at bar `i` uses only the trailing `lookback_bars` basis values ending at
`i` (all known at close `i`); positions still open at series end are reported `unclosed` and **not** booked
(no optimistic mark). +8 unit tests pin the basis math, the inner-join alignment, the warmup bar count, the
rich→short / cheap→long direction with the perp-move−fee identity, and unclosed handling.

**Evidence — real HL history, BTC/ETH/SOL/HYPE, 1h, maker_fee=1bp, two disjoint 120d windows. `net` =
per-round-trip net-of-cost edge (bps):**

```
Param sweeps (pooled universe), trailing / older net bps/round-trip:
  entry_z (lb=48, exit=0.5):  1.5 → -1.92 / +14.15  (trailing SIGN-FLIPS)
                              1.75→ -1.89 / +12.00  (flip)
                              2.0 → +1.60 / +11.06  ← SIGN-STABLE+ (the only one)
                              2.25→ -4.94 / +14.44  (flip)
                              2.5 → -5.17 /  -1.59  (both negative)
  lookback (ez=2.0, exit=0.5): 24 → -0.91 / +11.45  | 36 → -1.77 / +12.41
                               48 → +1.60 / +11.06  | 72 → -3.52 /  +8.63  | 96 → +0.40 / +10.41
  exit_z (ez=2.0, lb=48):     0.0 →  0.00 /  0.00 (no trades) | 0.25 → -5.02 / +28.12
                              0.5 → +1.60 / +11.06 | 1.0 → +1.11 / -0.01

Per-coin decomposition at the lone sign-stable point (lb=48, ez=2.0, exit=0.5), net bps/RT:
  trailing:  BTC -6.97 | ETH +4.39 | SOL +3.85 | HYPE +5.24   (pool +1.60)
  older:     BTC +17.57| ETH +31.97| SOL +0.80 | HYPE -7.59   (pool +11.06)
```

**Honest read — a lead-shaped artifact, pruned.** At exactly lb=48 / entry_z=2.0 / exit_z=0.5 BOTH disjoint
windows are net-positive and sign-stable (+1.6 / +11.1) — the first sign-stable-positive candidate since
pairs. But it fails on two independent counts: **(1) knife-edge** — it is the *only* sign-stable-positive
point in the whole sweep; every neighbor flips the trailing window negative (entry_z 1.5/1.75/2.25/2.5,
lookback 24/36/72, exit_z 0.25/1.0), so it is a data-mined point, not a plateau. **(2) per-coin sign-flip /
averaging artifact** — the pooled positivity is not a coherent effect: BTC flips −7.0(trailing)→+17.6(older)
and HYPE flips +5.2→−7.6 across the two windows, and the older pool is ETH/BTC-specific (+32/+18) that does
not repeat in trailing. No constituent carries a durable basis edge; the pooled "+1.6/+11.1 sign-stable" is
the same portfolio-averaging illusion that broke pairs under leave-one-out (Iter 32/33). The trailing-window
majors basis is ~zero-to-marginal, **confirming REVIEW M5's prior** (majors basis tiny & well-arbitraged).

**Net — the tenth thesis is PRUNED.** Crucially, the basis universe **cannot be widened**: UBTC/UETH/USOL
+ HYPE are the *only* liquid perp/spot overlaps on HL (other spot tokens are HL-native memes without perps),
so there is no disjoint rescue basket to push to — unlike pairs/session, the prune is structurally complete
in one slice. Ten structurally-different theses now pruned (eight directional + one execution + one
cross-market), every one either net-negative after costs or non-durable (regime-/window-/param-specific) on
HL majors+alts at 1h cadence. `basis_reversion.py` stays as a measurement/model tool; nothing touches
capital, no live change.

**Evidence (gate).** `uv run pytest -q` → **220 passed** (+8 basis-reversion); `ruff check src tests
scripts` → clean. Edge numbers are measurement (live candle fetch, no `data/` writes committed). No strategy
in the live roster changed; no live mode enabled.

**What's next (loop).** The fresh-thesis search on HL's available data (1h+ perp candles, funding, the four
perp/spot overlaps) is now genuinely exhausted across direction, execution, and cross-market classes — ten
prunes with the same regime/window-sensitivity or net-negative-after-cost signature. The two honest
remaining directions are: (a) **sub-bar / event-driven execution** — re-test the fast MR/maker edges at the
cadence REVIEW C7 says they actually live at, which requires fetching trade-tick / L2 data (an
*infrastructure* slice, and the only direction that attacks a root cause no bar-marked backtest can rule
out); or (b) accept the search verdict and make the **negative-edge finding itself the deliverable** —
pivot to Path C honest measurement / paper track-record (the supervisor/accounting/risk chassis is the
strong part of the repo, per REVIEW). The next iteration should pick one; (a) is higher-leverage if
trade-tick history is reachable, else (b) is the honest close.

## Iteration 39 — 2026-06-08 — B-cadence-data: is fine-cadence (5m/15m/1m) durability research runnable on HL candles? NO — blocked by data RETENTION, not tooling. (+ correct backward candle paginator)

**Context.** Ten theses are pruned. Iter 37/38 named two honest next directions: **(a)** sub-bar /
cadence-mismatch re-test (REVIEW C7 — signal horizon ≫ 1h action cadence; the only direction that attacks a
root cause no bar-marked backtest can rule out, flagged highest-leverage *if* finer data is reachable), and
**(b)** pivot to Path C honest measurement. This iteration tested whether (a) is actually reachable on HL
candle data before committing to a fine-cadence thesis — i.e. can we get the durability bar's 2× disjoint
~120d windows at 5m/15m?

**What I probed (real HL `candleSnapshot`).** Two structural limits, both measured:

```
Per-request cap (req a huge window, count what comes back):
  1m  req=10d  -> 5213 candles, span  3.6d   (oldest 2026-06-05)
  5m  req=60d  -> 5043 candles, span 17.5d   (oldest 2026-05-22)
  1h  req=300d -> 5003 candles, span 208.4d  (oldest 2025-11-12)
  => hard cap ~5000 bars/request, regardless of requested span.

Anchoring + retention (request OLD windows fully in the past):
  1h  [now-300d, now-250d]  -> EMPTY        (no 1h data older than ~208d)
  1h  [now-400d, now-100d]  -> 2603, oldest 2025-11-12, newest 2026-03-01
       => anchored to endTime (newest is exactly the requested end), startTime only a floor;
          oldest is the SAME ~208d-ago floor as the trailing call — no older data exists.
  5m  end=-20d  -> EMPTY  | 5m end=-30d -> EMPTY   (no 5m older than ~17.5d)
  15m end=-60d  -> EMPTY                            (no 15m older than ~52d)
```

**Finding (the deliverable).** HL `candleSnapshot` (1) caps at ~5000 bars/request AND (2) is **anchored to
`endTime`**, returning the most-recent block up to the requested end with `startTime` acting only as a
floor; and crucially (3) **HL retains only ~one cap of history per interval, total** — there is no older
data to page to in *any* direction. So the calendar coverage per interval is fixed: **1m ≈ 3.6d, 5m ≈ 17.5d,
15m ≈ 52d, 1h ≈ 208d.** The durability bar needs **two disjoint ~120d windows**; at any sub-1h cadence that
is **structurally impossible** (5m gives 17.5d *total*, 15m 52d *total* — not even one 120d window). This
also re-explains Iter-35's "1h baseline can't extend past ~240d" as a **hard retention ceiling, not a fetch
bug**, and confirms the parked B-exec-tickmark refinement is retention-blocked too (no historical fine
candles, same as no historical L2/trade-tick).

**Conclusion — direction (a) via HL candles is dead.** Fine-cadence backtesting would require an *external*
tick/candle archive — either forward-recording our own WS stream over months, or a 3rd-party historical
feed — which is an infrastructure project, not a candle fetch, and yields no edge number for a long time.
That decisively re-weights the search to **(b) Path C honest measurement / paper track-record** as the next
*unblocked, evidence-producing* move: the supervisor/accounting/risk chassis is the strong part of the repo
(REVIEW), and the ten-thesis negative-edge result is itself a publishable finding.

**What changed (code, with tests).** Even though retention currently bounds it to one block, I shipped the
*correct* candle paginator for HL's endTime-anchored API: split the single request into `_fetch_candle_page`
and made `fetch_candles` page **backward** via new pure `_paginate_candles` (each page ends one
`interval_ms` before the oldest candle seen; dedupes by open time `t`; terminates on empty page / no new
(older) rows / reaching the `start_ms` floor / no time progress; returns oldest-first). This replaces the
prior single-shot fetch that silently truncated to one block and is the right shape if HL ever extends
retention or for any coin whose retained history exceeds one cap. `load_frames`/`cached_or_fetch` are
unchanged (they call `fetch_candles`); `build_frames` already sorts, so this is fully backward-compatible
— just robust and honest about the cap. +3 unit tests (backward reassembly of a >2-cap window mirroring
HL's `end`-anchored semantics; sub-cap single-page short-circuit; clean termination when no older history
exists rather than looping to `max_pages`). Live re-verify: 1h/300d→5003 span 208.4d, 5m/60d→5044 span
17.5d, 15m/120d→5015 span 52.2d — all 0 dupes, sorted, terminate cleanly.

**Evidence (gate).** `uv run pytest -q` → **223 passed** (+3 candle-pagination); `ruff check src tests
scripts` → clean. Probes are live measurement (no `data/` writes committed). No strategy in the live roster
changed; no live mode enabled.

**What's next (loop).** With direction (a) confirmed retention-blocked on HL candles, the honest
next-unblocked move is **(b) Path C**: turn the ten-thesis negative-edge finding into a clean
paper/measurement deliverable — e.g. a reproducible "edge search summary" report (every thesis, universe,
window, net-of-cost number, prune reason) generated from the recorded results, and/or harden the paper
track-record export (B15) into something public-grade. The only route back to (a) is sourcing an external
fine-cadence archive (forward-record WS now, or a 3rd-party feed) — a standalone infra bet, not a one-shot
iteration.

## Iteration 40 — 2026-06-08 — B-edge-summary: the ten-thesis negative-edge search as a publishable Path-C artifact (`hlbot edge-search`)

**Context.** Ten structurally-different theses are pruned (Iter 16→38) and fine-cadence durability research
is structurally retention-blocked on HL candles (Iter 39). Iter 38/39 both named the same honest
next-unblocked move: stop searching directions HL data can't support and turn the negative-edge result into
a clean **Path C** deliverable. An allocator (or the supervisor's own go-live gate) needs to know *what was
searched, on what universe, over what windows, and why each was rejected* — not just "no edge yet". The
search verdict is itself the product.

**What I built (pure, tested).** New `src/hl_bot/reports/edge_search.py` — the canonical, auditable record
of the whole search, as fixed data + rendering (no network, no DB; the search is over fixed history and the
verdicts are final). A frozen `Thesis` dataclass + `THESES` tuple encodes the **1→10 enumeration** exactly as
PROGRESS numbered it (the Iter-23 list fixes #1–5 = TWAP-MR, funding carry, x-sect momentum, regime-gated
momentum, ts-momentum; then #6 majors-1d momentum (Iter 27 "sixth"), #7 pairs (Iter 29 "seventh"), #8
session-timing (Iter 34 "eighth"), #9 maker execution (Iter 36 "ninth"), #10 perp/spot basis (Iter 38
"tenth")). Each row carries its **backlog id, PROGRESS iteration(s), universe, durability bar, recorded
net-of-cost headline, and prune reason**, so every number is checkable against this log. A `SEARCH_BOUNDARY`
string records the Iter-39 retention finding (why the search is *exhausted*, not merely paused).
`build_edge_search()` → machine-readable dict with a summary (n_theses, n_pruned, by-class breakdown,
verdict); `to_markdown()` renders the allocator-grade table; `export()` writes `edge_search.{json,md}`. Wired
as `hlbot edge-search` (mirrors the `track-record` command shape; read-only, no DB touch).

**Faithfulness gate (why this is honest, not spin).** The class breakdown is asserted in a test to equal the
Iter-38 narrative — **8 directional + 1 execution + 1 cross-market** — and the numbering is asserted gap-free
1→N with unique backlog keys, so the artifact cannot silently drift from the recorded search. Every headline
is transcribed from its cited iteration (e.g. carry alts oos −16.8/−33.2bps; regime-momentum +8.4→−7.8bps
out-of-time; pairs 3-basket +5.3/+8.2 only-PASS; maker symmetric −2.6..−3.3bps/fill; basis +1.6/+11.1 lone
knife-edge). The summary verdict is the literal search result: **0 of 10 deployable — every candidate
net-negative after costs or non-durable (regime-/window-/param-specific) on HL majors+alts.**

**Evidence (gate).** `uv run pytest -q` → **230 passed** (+7 edge-search: 1→N numbering no gaps; unique
backlog keys; class breakdown = 8/1/1; all-pruned invariant; markdown renders every row; JSON/MD export
round-trip; non-empty headline+reason on every row). `ruff check src tests scripts` → clean. `hlbot
edge-search` runs and writes both files. No `data/` writes committed; no strategy/roster/live-mode change —
pure reporting over the recorded results.

**What's next (loop).** Path C is now the live track. Two honest follow-ons: (a) fold the edge-search
summary into the public track-record bundle (one `track-record`-style command that emits chassis evidence +
the negative-edge finding together — the complete allocator packet), and/or harden B15's track-record with
the chart export it still TODOs. The only route back to *finding* an edge is sourcing an external
fine-cadence archive (forward-record WS now, or a 3rd-party feed) — a standalone infra bet (B-exec-tickmark /
direction (a)), not a one-shot iteration. This iteration makes the search's negative verdict a first-class,
reproducible deliverable rather than prose buried in this log.

## Iteration 41 — 2026-06-08 — B-allocator-packet: chassis + track record + edge search in one bundle (`hlbot allocator-packet`)

**Context.** Iter 40 shipped the edge-search artifact and named the honest next-unblocked Path-C move (a):
fold it into a single allocator-grade bundle alongside the chassis evidence and the live track record. The
two pure reports already existed separately (`track_record` = live numbers, `edge_search` = the ten-thesis
negative result), but an allocator (or the supervisor's own go-live gate) had to read three scattered files
to get the whole picture: *here is the machine, here is its live record, here is everything we tried and
rejected.* The complete packet is the deliverable.

**What I built (pure, tested).** New `src/hl_bot/reports/allocator_packet.py` — it **composes** the two
existing reports and adds one new section, introducing **no numbers of its own**. A frozen `ChassisItem`
dataclass + `CHASSIS` tuple encodes the REVIEW "What's good (keep it)" strengths — cloid attribution
(`agents/cloid.py`), ground-truth accounting (`ingest/hyperliquid.py`), order safety (`exec/orders.py`),
supervisor semantics (`supervisor/goals.py`), risk scaling (`risk/scaling.py`), research hygiene
(`research/strategy_health.py`) — each citing a **real source module** (all six verified present at
authoring; a test re-asserts every cited path is a file so the chassis claim can't silently rot).
`build_allocator_packet(conn)` → `{generated_ms, headline, chassis, track_record, edge_search}` (calls
`build_track_record` + `build_edge_search`); `to_markdown` renders the chassis table then splices in
`track_record.to_markdown` + `edge_search.to_markdown` verbatim (sub-reports keep their own headers, so the
packet can never drift from the canonical reports); `export` writes `allocator_packet.{json,md}`. Wired as
`hlbot allocator-packet` (read-only on the DB, mirrors the `track-record`/`edge-search` command shape).

**Honesty gate (why this is evidence, not a pitch).** The `HEADLINE` states plainly that capital is NOT
warranted until a strategy clears the durability bar (G0–G3) — the packet is the evidence the go-live gate
references, explicitly "not a solicitation." It carries the literal search verdict (0 of 10 deployable) and
the same `score_agent`-derived live numbers used in production, so it cannot flatter reality.

**Evidence (gate).** `uv run pytest -q` → **234 passed** (+4 allocator-packet: chassis sources are real
files; both sub-reports carried faithfully [thesis count + all-pruned, live agent present]; markdown renders
all three sections + every chassis source; JSON/MD export round-trip). `ruff check src tests scripts` →
clean. `hlbot allocator-packet` runs and writes both files (under gitignored `data/`, nothing committed). No
strategy/roster/live-mode change — pure composition over the recorded reports.

**What's next (loop).** Path C now has its single allocator deliverable. Remaining P3 items are genuine
spikes gated behind a real edge: B16 (HL vault evaluation — requirements/fees/risk, gated behind a G3 track
record) and B17 (moonshot sleeve spec — ring-fenced, loss-bounded, spec-only). Both are design/spec work, no
edge required. The only route back to *finding* an edge is sourcing an external fine-cadence archive
(forward-record WS now, or a 3rd-party feed) — a standalone infra bet (direction (a) / B-exec-tickmark), not
a one-shot iteration. This iteration completes the "publish the honest state" arc the last two iterations
started.

## Iteration 42 — 2026-06-08 — B-fine-record: forward-record fine-cadence candles (`hlbot record-trades`) — the only route back to finding an edge

**Context.** The edge search is exhausted on HL *historical* data: ten structurally-different theses pruned
(Iter 16→38) and — critically — Iter 39 proved the dead "direction (a)" (REVIEW C7: sub-bar / cadence
mismatch, the highest-leverage *unrun* angle) is **structurally un-backtestable** from HL's candle API,
because HL retains only ~one `candleSnapshot` cap of history total (~3.6d at 1m, ~17.5d at 5m, ~52d at 15m).
There is no older fine-cadence data to fetch. Iter 39/40/41 each closed naming the **same** honest move:
the only route back to *finding* an edge is to **forward-record our own fine candles now**, so that months
from now a 1m/5m archive exists that the existing backtester can replay. The last three iterations shipped
the Path-C reporting that publishes the negative result (edge-search, allocator-packet); that arc is done.
This iteration starts the one thing that can re-open the search.

**What I built (pure core + thin loop + tested).** New `src/hl_bot/backtest/recorder.py`, split like
`ingest/ws.py` into a pure, unit-tested core and a thin network loop:
- `TradeCandleAggregator(interval)` — folds a trade stream (the dicts `MarketState` already produces from the
  WS `trades` channel: coin/px/sz/ts) into OHLCV candles, emitting the **exact same dict shape**
  `fetch_candles` returns: `{coin,t,T,o,h,l,c,v,n}`. `open`/`close` are tracked by trade **timestamp, not
  arrival order**, so mildly out-of-order WS delivery still yields the correct first/last price; `h/l/v/n`
  fold naturally. `flush_completed(now_ms)` pops every bucket strictly older than the bucket containing
  `now_ms` (so the still-filling bucket is retained) → bounded memory over a long run; `pending_candles()`
  peeks without removing.
- `append_candles` / `load_recorded_candles` — an **append-only** JSONL archive (plain or `.gz`) under
  gitignored `data/`. Append-only so a restarted recorder never rewrites history; `load_recorded_candles`
  dedupes by (coin, open time `t`) **keeping the last line written** (a re-flushed bucket's newer value wins)
  and returns a `candles_by_coin` dict that is a **drop-in for `data.build_frames`**. This closes the loop
  recorded-JSONL → `build_frames` → the existing backtester with **zero adaptation**.
- `run_recorder` — the thin WS connect loop (subscribes `{"type":"trades","coin":...}` per coin via the HL
  SDK `Info`, folds each trade, appends completed candles every `flush_interval_s`; `pragma: no cover`).
- Wired as `hlbot record-trades --coins BTC,ETH,SOL,HYPE --interval 1m --archive data/recorder/trades_1m.jsonl`
  (`seconds=0` = forever), mirroring the `hlbot ws` command shape; supervise via systemd next to `hlbot-ws`.

**Why this is the right shape.** The whole reason fine-cadence research died (Iter 39) is *data retention*,
not tooling — so the fix is a data-accumulation service, and the highest-value property is that the recorded
output is **byte-compatible with the backtester's input** (same candle dict, loader returns `candles_by_coin`)
so no future glue is needed: the day enough data exists, the retention-blocked theses (B-exec-tickmark,
sub-bar durability) run unchanged. The end-to-end test asserts exactly this: 60 synthetic 1m trades →
aggregate → append → `load_recorded_candles` → `build_frames` produces real frames with `BTC` mids.

**Evidence (gate).** `uv run pytest -q` → **244 passed** (+10 recorder: interval flooring; basic OHLCV across
two buckets; open/close track trade-time under reordering; bad-trade rejection; `flush_completed` leaves the
current bucket then releases it; multi-coin separation; unknown-interval `ValueError`; append+load dedup
last-write-wins; empty append / missing file; end-to-end recorded→`build_frames`). `ruff check src tests
scripts` → clean. `hlbot record-trades --help` registers. No `data/` writes committed; **no strategy, roster,
or live-trading change** — this records public market data only; nothing places an order.

**What's next (loop).** This is slice 1 (the machine). The remaining work is *operational, not a one-shot
iteration*: (2) add `record-trades` to the deploy systemd units so it runs 24/7 alongside `hlbot-ws`, and (3)
once ~30–60d of 1m/5m data has accumulated, re-run the sub-bar execution + fine-cadence durability theses the
retention ceiling blocked. Until that archive fills, the search remains exhausted on HL historical data —
this iteration just guarantees that the *next* attempt has data to run against, which it otherwise never
would (every iteration of delay is a month of fine candles permanently lost to retention).

## Iteration 43 — 2026-06-08 — B-fine-record slice 2: run `record-trades` 24/7 under systemd

**Context.** Iter 42 built the recorder machine (pure `TradeCandleAggregator` + append-only JSONL archive +
`hlbot record-trades`), but it only matters if it actually *runs* — HL retains ~one candle cap of history, so
every day the recorder is NOT running is a day of fine candles permanently lost to retention. Iter 42's named
slice 2 was to wire it into the deploy systemd units so the archive accumulates on every deployed host. This
is that slice: pure deploy plumbing, no strategy/runtime change.

**What I changed.** New `deploy/systemd/hlbot-recorder.service`, mirroring `hlbot-ws.service`: `Type=simple`,
`Restart=always`/`RestartSec=10` (long-running supervised loop), the same sandbox (`NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=/opt/hl-bot/data`, `PrivateTmp`), and an
env-driven `ExecStart` (`uv run hlbot record-trades --coins $HLBOT_RECORD_COINS --interval
$HLBOT_RECORD_INTERVAL --archive $HLBOT_RECORD_ARCHIVE`, defaults BTC,ETH,SOL,HYPE / 1m /
data/recorder/trades_1m.jsonl) so the operator tunes it without touching the unit. The header states plainly
it records public market data only and never places an order.
- `install.sh`: added `hlbot-recorder.service` to the `systemctl enable --now` line (so a fresh install — and
  AWS user-data, which just calls install.sh — boots the recorder alongside ws/tick/report/update) and updated
  the confirmation log line.
- `update.sh`: added `hlbot-recorder.service` to the post-green-tests restart list, so the self-updating box
  restarts the recorder on every deployed commit (picks up new coins/code without a manual restart).
- `env.example`: documented `HLBOT_RECORD_COINS` / `HLBOT_RECORD_INTERVAL` / `HLBOT_RECORD_ARCHIVE` with a
  note on why (HL retention ceiling → forward-record is the only route to a months-long 1m/5m dataset).
- `deploy/README.md`: added the recorder row to the units table.

**Evidence (gate).** `uv run pytest -q` → **250 passed** (+6 `tests/test_deploy_recorder.py`: unit file
exists; ExecStart runs `hlbot record-trades` and references all three env vars; long-running + sandboxed
[`Type=simple`/`Restart=always`/`ReadWritePaths=data`]; install.sh enables it; update.sh restarts it;
env.example documents the vars — these guard against the unit silently drifting out of the deploy wiring).
`ruff check src tests scripts` → clean. `bash -n` on both shell scripts → syntax OK. No `data/` writes
committed; **no strategy, roster, or live-trading change** — deploy plumbing only.

**What's next (loop).** The recorder now accumulates 1m candles 24/7 on every deployed host. Remaining
work on this thread is purely the passage of time + slice 3: once ~30–60d of 1m/5m data has built up,
re-run the sub-bar execution + fine-cadence durability theses the HL retention ceiling blocked (Iter 39;
B-exec-tickmark overlaps) against the recorded archive (drop-in for `build_frames`, no glue). Until that
archive fills, the edge search stays exhausted on HL historical data — but the data is now being captured
rather than lost. Other unblocked tracks: P3 spikes B16 (HL vault eval) / B17 (moonshot sleeve spec), both
design-only.

## Iteration 44 — 2026-06-08 — B-lowvol: cross-sectional low-volatility (betting-against-volatility) — the ELEVENTH thesis, PRUNED

**Context.** Ten structurally-different theses are pruned (Iter 16→38) and the fine-cadence direction is
retention-blocked on HL candles (Iter 39); the recorder now forward-captures 1m data for a *future* slice-3
re-run (Iter 42/43), but that thread is data-/time-gated, not a one-shot iteration. The operator's standing
preference is real-data edge work over reporting/governance polish, so rather than another Path-C report I
tested **one** genuinely new, a-priori-motivated, structurally-different thesis that the search had never
touched: the **cross-section of realized volatility**. Every prior candidate keyed off price *return*
(TWAP-MR, x-sect/ts/majors-1d momentum), *funding level* (carry), a *pairwise ratio* (pairs), a *cross-market
gap* (basis), the *clock* (session), or *microstructure* (maker spread) — none off **how volatile each coin
is**. The low-volatility anomaly (Frazzini-Pedersen "betting against beta") is one of TradFi's most robust
cross-asset factors (low-risk names deliver higher *risk-adjusted* returns because leverage-constrained
buyers overpay for high-beta), and realized vol is far more *persistent* bar-to-bar than realized return —
the exact property that sign-flipped the momentum leads across disjoint windows — so it was worth a clean
durability test, not a guess.

**What I built (pure, tested).** New `src/hl_bot/agents/xsect_lowvol.py` (`XSectLowVolAgent` /
`XSectLowVolConfig`), mirroring the `xsect_momentum` machinery but with the signal swapped from return-rank to
**realized-vol rank**: `_realized_vol(closes, lb)` = sample std (ddof=1) of the last `lb` log-returns;
dollar-neutral LONG the calmest `top_k` / SHORT the wildest `top_k`, requiring ≥`2*top_k` eligible coins to
form both legs; exits when a coin drops out of the rank set or switches legs; an `invert` flag flips the legs
(LONG high-vol / SHORT low-vol — the "lottery-demand / high-vol-chase" mirror) at no extra code. Registered in
both `confirm`/`backtest` CLI factories. +7 unit tests (longs calmest/shorts wildest; invert flips both legs;
needs ≥2*top_k coins else holds; thin coins filtered; short series holds; realized vol == manual log-return
std; higher oscillation → higher vol).

**Result (real HL cache, 1h, maker_fee=1bp, `confirm --windows 2 --prefer maker`, two disjoint 120d windows).**
- **majors (AVAX,BTC,ETH,HYPE,LINK,SOL) base (low-vol):** ❌ NOT DURABLE — full-sample **net-NEGATIVE &
  sign-stable** (trailing −32.7bps, in −5.4/oos −134.1; older −8.5bps, in +16.1/oos −76.7). This is the
  *cleanest prune signature*: BAB is the **wrong sign** in crypto majors — high-vol coins outperform low-vol
  (risk-on beta / lottery-demand dominates the leverage-constraint premium).
- **majors invert (high-vol chase):** ❌ NOT DURABLE but the mirror — full-sample **positive & sign-stable**
  (trailing +21.3, older +5.3) yet blocked by within-window walk-forward (regime-sensitive: trailing oos
  +97.2 but in only +5.0; older in −18.2). Same "sign-stable lead, not deployable" bucket as the majors-1d
  momentum and session leads — not the cross-window artifact sign-flip, but not durable either.
- **high-funding alts (AERO,APT,EIGEN,INJ,NIL,PURR,PYTH,S,SPX,TRUMP) base (low-vol):** ❌ NOT DURABLE —
  marginally positive full-sample (trailing +5.9, older +4.3) but **OOS negative in BOTH windows** (oos −1.2
  / −15.7), the regime-sensitive failure mode.

**Conclusion.** The eleventh thesis does not clear the durability bar on either universe: **wrong sign on
majors** (the right-sign mirror is only a regime-sensitive lead) and a **regime-sensitive non-durable** result
on alts. The volatility cross-section joins the pruned set; `xsect_lowvol_v1` stays in the roster for
paper/measurement only. Maker-only, no live change.

**Evidence (gate).** `uv run pytest -q` → **257 passed** (+7 xsect_lowvol). `uv run ruff check src tests
scripts` → clean. Backtests ran fully offline against the existing gitignored cache (no `data/` writes
committed). No strategy promoted, no roster live-mode change.

**What's next (loop).** Eleven structurally-different theses now pruned on HL historical data; the search
remains exhausted on what HL candle history can support. The only route back to *finding* an edge is the
fine-cadence archive the recorder is now accumulating (slice 3, data-/time-gated). Genuinely-new untouched
classes are getting scarce — remaining candidates would be combinations of pruned signals (not structurally
new) or require data HL doesn't retain (sub-bar/tick). Remaining unblocked non-edge tracks are the P3 design
spikes (B16 HL vault eval, B17 moonshot sleeve spec), both design-only and gated behind a real edge before
any capital.

## Iteration 45 — 2026-06-08 — B-illiq: cross-sectional illiquidity (Amihud premium) — the TWELFTH thesis, a GENUINE LEAD

**Context.** Eleven structurally-different theses are pruned (Iter 16→44) and the fine-cadence direction is
retention-blocked on HL candles (Iter 39; the recorder forward-captures 1m data for a future slice-3 re-run,
but that thread is data-/time-gated). The operator's standing preference is real-data edge work over
reporting/governance polish, so rather than a Path-C report I tested **one** genuinely-new, a-priori-motivated,
structurally-different thesis the search had never touched: the **cross-section of liquidity / trading
volume**. Every prior candidate keyed off price *return* (TWAP-MR, x-sect/ts/majors-1d momentum), *funding
level* (carry), a *pairwise ratio* (pairs), a *cross-market gap* (basis), the *clock* (session),
*microstructure* (maker spread), or *realized-vol rank* (low-vol) — **none off volume**. The Amihud (2002)
illiquidity premium is — alongside size/value/momentum/low-vol — one of TradFi's most robust a-priori
cross-asset factors: less-liquid assets must compensate holders for price-impact / inventory risk, so they
earn higher expected returns. And liquidity is *even more persistent* bar-to-bar than realized vol — the
property that sign-flipped the momentum leads — so it was worth a clean durability test, not a guess.

**What I built (pure, tested).** New `src/hl_bot/agents/xsect_illiq.py` (`XSectIlliqAgent` / `XSectIlliqConfig`),
mirroring the `xsect_lowvol` machinery but with the signal swapped to **Amihud illiquidity**:
`_illiquidity(closes, dollar_vol, lb)` = mean(|log-return| over the last `lb` bars) / dollar-volume — price
impact per dollar of volume; dollar-neutral LONG the most-illiquid `top_k` / SHORT the most-liquid `top_k`,
requiring ≥`2*top_k` eligible coins to form both legs; exits when a coin drops out of the rank set or switches
legs; an `invert` flag flips the legs (liquidity-chase mirror) at no extra code. **Uses only data the cached
frames already carry** (`day_ntl_vlm` as the dollar-volume normalizer, `closes` for the |returns|) — zero new
data plumbing, so it runs fully offline against the existing cache. Registered in both `confirm`/`backtest`
CLI factories. +8 unit tests (longs most-illiquid/shorts most-liquid; invert flips both legs; needs ≥2*top_k
coins else holds; thin coins filtered by the volume gate; short series holds; illiq == manual |log-ret|/vol
formula; illiq rises with |return| and falls with volume; zero-volume → None).

**Result (real HL cache, 1h, maker_fee=1bp, `confirm --windows 2 --prefer maker`, two disjoint 120d windows).**
- **high-funding alts (AERO,APT,EIGEN,INJ,NIL,PURR,PYTH,S,SPX,TRUMP) base:** **✅ DURABLE** — positive maker
  edge in BOTH disjoint windows *in AND oos* (trailing +42.0bps in +18.7/oos +98.7; older +13.2bps in
  +5.3/oos +67.0). **The first candidate to clear the full canonical durability bar since pairs (Iter 29)**,
  and at *sane* per-round-trip magnitudes (not a static-tilt artifact).
- **held-out alts (AAVE,ARB,ENA,JUP,LDO,OP,SEI,SUI,TIA,WLD — disjoint basket):** ❌ NOT DURABLE but
  **sign-stable POSITIVE both windows** (trailing +35.3 in +35.0/oos +54.0; older +8.5, blocked only by the
  older window's marginal in −1.9 walk-forward). **Crucially it does NOT sign-flip on a disjoint basket** —
  the exact failure that killed pairs (held-out −4.7, Iter 31) and the momentum leads (alt-basket sign-flips).
  This is a materially stronger cross-basket generalization signal than any prior lead.
- **majors (AVAX,BTC,ETH,HYPE,LINK,SOL) base:** ❌ NOT DURABLE but sign-stable-positive at *huge/static*
  magnitudes (+1314.8/+116.6) — when volume dispersion is extreme (BTC/ETH ≫ LINK), the illiq rank barely
  rotates, so the book degenerates into a near-static LONG-low-volume-coin / SHORT-BTC **directional** bet
  (confounded with the specific coins, not a clean factor harvest). The "lead, not artifact" bucket but not a
  clean test.
- **majors invert (liquidity-chase mirror):** ❌ cleanly NEGATIVE both windows (−1316.8/−118.6) — the exact
  mirror of the majors base, so the signal is **internally coherent** (a real ranked effect, not noise).

**Conclusion.** The twelfth thesis is a **genuine LEAD — the strongest cross-basket result of the entire
search.** It DURABLY clears the canonical bar on the high-funding alts basket AND stays sign-stable-positive
on a *disjoint* alt basket (no sign-flip), with a coherent majors mirror. This is the first thesis since pairs
to clear the bar, and unlike pairs it does not immediately collapse on a held-out basket. **But it is NOT a
deploy:** the durability bar is necessary-not-sufficient (pairs passed it then failed leave-pairs-out,
leave-one-out, AND diversification, Iter 31/32/33), and a key open confound is whether the alts edge is the
*liquidity factor* or simply that the low-volume alts were these windows' pumpers (a size/momentum confound).
`xsect_illiq_v1` stays maker-only paper; nothing touches capital.

**Evidence (gate).** `uv run pytest -q` → **265 passed** (+8 xsect_illiq). `uv run ruff check src tests
scripts` → clean. Backtests ran fully offline against the existing gitignored cache (no `data/` writes
committed). No strategy promoted, no roster live-mode change.

**What's next (loop).** Push the lead, don't deploy it — the same disciplined slice sequence that pruned
pairs: (1) plateau-sweep `illiq_lookback` (is the alts PASS a param knife-edge?); (2) leave-one-coin-out on
the alts basket (is it one lucky coin?); (3) the liquidity-vs-confound decomposition (regress the alts edge
against trailing return / size to see if it's really the Amihud premium or just low-vol-coin momentum); (4)
longer baseline / `--windows 3`; (5) strip the majors static directional tilt to test whether a *rotating*
illiq signal survives on majors. The first of these (param plateau + leave-one-coin-out) is the highest-value
next iteration — if the alts PASS survives both, this becomes the best edge candidate the search has produced.

## Iteration 46 — 2026-06-08 — B-illiq push slices (1)+(2): lookback plateau + leave-one-coin-out

**Context.** The twelfth thesis (cross-sectional Amihud illiquidity) is the strongest cross-basket result of
the entire search (Iter 45): ✅ DURABLE on high-funding alts AND sign-stable-positive on a disjoint alt basket.
The durability bar is necessary-not-sufficient (pairs cleared it then died on leave-pairs-out / leave-one-out /
diversification), so before any deploy talk the lead must survive the same disciplined stress sequence that
pruned pairs. This iteration ran the two highest-value push-slices the Iter-45 "what's next" named: (1) the
`illiq_lookback` plateau-sweep (is the alts PASS a param knife-edge?) and (2) leave-one-coin-out (is it one
lucky coin?). Slice (2) needed new machinery — a cross-sectional book has no per-coin config knob, so unlike
pairs (`leave_one_pair_out` edits the `pairs=` spec) the coin must be dropped from the *data*.

**What I built (pure, tested).** Two pure helpers in `backtest/confirm.py`:
- `coins_in_frames(frames)` — deduped, order-preserving union of coins in the frames' `mids` (the universe).
- `drop_coin(frames, coin)` — a copy of the frames with `coin` removed from every per-coin field (mids,
  funding, day_ntl_vlm, open_interest, candles_1h, closes, spot_mids) via `dataclasses.replace`; `liquidations`
  (an event list, not coin-keyed) carried through, inputs untouched. The cross-sectional analogue of
  `leave_one_pair_out`, reusable for ANY cross-sectional candidate (momentum / low-vol / illiq).
Wired a `confirm --leave-one-coin-out` flag (cross-sectional agents, `--windows>=2`): loads windows once, then
runs the durability bar on the full universe + each universe with one coin dropped from every window, printing
durable/not + per-window edges (mirrors the pairs `--leave-one-out` block). +6 unit tests (universe dedup/union;
drop removes from every field; drop is pure; ts/liquidations preserved; absent-coin no-op copy).

**Result (real HL cache, alts_highfunding, 1h, maker_fee=1bp, `--windows 2 --prefer maker`, two disjoint 120d).**
- **Slice (1) `illiq_lookback` plateau-sweep {24,36,48,72,96}:** ❌ NO PLATEAU (knife-edge). Only **lb=48**
  clears the full durability bar; neighbours 24/36 are NOT DURABLE (though trailing full-sample edge is smoothly
  **positive and rising**: 24 +32.2 / 36 +37.9 / 48 +42.0), and 72/96 trade-starve (no edge). So the binary
  PASS is a lookback knife-edge — the *sign* is stable across lookbacks but the *durable PASS* sits at one value
  (same fragility signature as the pairs entry_z knife-edge, Iter 30).
- **Slice (2) leave-one-coin-out (lb=48):** the full basket is ✅ DURABLE (+42.0/+13.2); dropping a single coin
  stays DURABLE for **AERO (+36.1/+14.8), EIGEN (+61.4/+16.6), S (+72.0/+24.7)** and demotes to NOT-DURABLE for
  the other 7 (INJ/PURR/TRUMP/NIL/APT/SPX/PYTH). **The decisive property: NO single-coin drop sign-flips** —
  every drop is **positive in BOTH windows** (trailing +14.0…+72.0, older +2.3…+24.7). The 7 demotions are all
  via within-window walk-forward (regime-sensitive), NOT the cross-window sign-flip. This is materially
  stronger than pairs, whose leave-one-out *sign-flipped* (LINK/AAVE alone and the ETH/BTC|LINK/AAVE pair-out
  went negative, Iter 32). So the illiq alts edge is a genuine **pooled** cross-sectional effect — not carried
  by one lucky coin — and stays sign-stable-positive under any single removal.

**Conclusion.** The lead SURVIVES leave-one-coin-out in the way that matters (no sign-flip, every subset
positive both windows) — the failure mode that killed pairs does NOT appear here. But the binary durability
PASS is fragile in *both* dimensions probed: a lookback knife-edge (only lb=48) and a basket-composition
knife-edge (only full + 3 of 10 drops clear the full bar; the rest stay positive but fail within-window
walk-forward). Net verdict unchanged from Iter 45: a **genuine, still-best-in-search LEAD, not a deploy** —
the edge is real and sign-robust, but the deployable-PASS is over-conditioned (lb≈48, full basket). The next
disciplines that could promote or prune it are the confound decomposition (is it the liquidity factor or just
low-volume alts being the windows' pumpers?) and a longer baseline. `xsect_illiq_v1` stays maker-only paper;
nothing touches capital.

**Evidence (gate).** `uv run pytest -q` → **271 passed** (+6 leave-one-coin-out). `uv run ruff check src tests
scripts` → clean. All backtests offline against the existing gitignored cache (no `data/` writes committed).
No strategy promoted, no roster live-mode change.

**What's next (loop).** Two slices done; remaining illiq push-slices from Iter 45: (3) the **liquidity-vs-
confound decomposition** — regress the alts edge against trailing return / size to test whether it's the Amihud
premium or just the low-volume alts being these windows' pumpers (size/momentum confound); this is now the
highest-value next iteration because the lead is sign-robust but the *economic interpretation* is the open
question that decides deploy-vs-prune. (4) longer baseline / `--windows 3`; (5) strip the majors static
directional tilt. The `drop_coin`/`coins_in_frames` machinery is now reusable to retro-run leave-one-coin-out
on any prior cross-sectional thesis if needed.
