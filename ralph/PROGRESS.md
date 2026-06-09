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
