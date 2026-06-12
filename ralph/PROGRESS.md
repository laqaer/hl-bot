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

## Iteration 9 — 2026-06-08 — per-agent drawdown so guardrails can fire (C5/B7)

**Context.** Honest-measurement leverage (#3). Even after Iter 7 added per-agent
Sharpe, per-agent `max_drawdown`/`calmar` were still `None` for every real agent
(only `_account` had them). So `funding_arb_v1.yaml`'s demote-on-7d-drawdown>10%
guardrail was *permanently N/A and could never fire* — a risk control that looked
present but did nothing. The twap configs even documented the caveat.

**Changed (1 commit).**
- **metrics.py** — new `_daily_pnl_drawdown(daily, capital_base)` builds a
  synthetic equity curve `capital_base + cumsum(daily_pnl)` and returns fractional
  max-drawdown + Calmar (same units the account curve uses, i.e. what a `-0.10`
  guardrail compares against). `score_agent` gains a `capital_base` param; the
  per-agent branch now sorts the daily series chronologically (cumsum needs order)
  and fills dd/calmar when a base is given.
- **goals.py** — `AgentGoals` gains a `capital` field; `evaluate` threads it into
  `score_agent(capital_base=g.capital)`.
- **configs** — `funding_arb_v1.yaml` sets `capital: 1000` so its drawdown
  guardrail is evaluable; fixed the now-stale "max_drawdown is account-only" NOTE
  in `twap_mr_v1.yaml`.
- **tests** — `test_per_agent_drawdown_needs_capital_base` (N/A without base, −25%
  with base on a +100/+100/−300 series) and `test_drawdown_guardrail_can_fire`
  (the guardrail now returns fail/demote instead of N/A).

**Evidence.** 91 → **93 tests pass**; `ruff check src tests scripts` clean.

**What's next (loop).** B9 (fills→positions replay so attribution survives partial
fills, M2), B12 (consolidate the two execution paths, M3), userFills WS for
instant maker-fill detection, B4-RUN (confirm carry on real history — network-gated).

---

## Iteration 10 — 2026-06-08 — fills→positions replay (B9 / M2)

**Context.** Honest-measurement leverage (#3). The `positions` table has existed
in the schema since day 0 ("Updated from fills on ingest") but was **never
populated** — per-agent attribution was inferred only from the binary
place/flatten decision log, so partial fills, size drift, and manual interference
weren't tracked (REVIEW M2). The decision-log heuristic can't tell a half-filled
entry from a full one, or notice a manual trim.

**Changed (1 commit).**
- **`scoring/positions.py`** — `replay_positions(fills)` is a pure per-(agent,coin)
  state machine: signed `net_sz` (B=+, A=−), size-weighted `avg_entry_px` that
  (a) weights on same-side adds, (b) is preserved on partial closes, (c) clears
  to 0 on full close, (d) resets to the fill price on a flip through zero; plus
  accumulated `realized_pnl` (taken straight from exchange `closed_pnl`, never
  invented) and `fees_paid`. A `_EPS` snaps float-dust residuals to flat.
  `rebuild_positions(conn)` replays the full fills history (ordered by time_ms,
  tid) and rewrites the table — idempotent, cheap, always reflects ground truth.
- **CLI** — `hlbot ingest` now calls `rebuild_positions` after fills/funding (so
  the table stays current on every pull); new `hlbot positions` command displays
  per-agent net size / avg entry / realized / fees.

**Evidence.** 93 → **102 tests pass** (9 new: weighted entry on partial fills,
preserved entry on partial close, clear-on-close, flip-through-zero, agent/coin
separation, idempotent rebuild, time-ordering-not-insertion-order, null→manual);
`ruff check src tests scripts` clean; `hlbot positions` registers and runs.

**Why it matters.** Attribution now derives from the exchange's fills, not a
heuristic — the foundation the supervisor trusts. Next this unlocks funding
attribution by *held size* (vs the current equal-split-among-holders in
`_agent_funding_payments`) and makes partial-fill PnL honest. No strategy/edge
claim here; this is measurement plumbing.

**What's next (loop).** B12 (consolidate `runtime.run_tick` vs `femr_tick`, M3),
userFills WS for instant maker-fill detection, B11 (feed/retire liq_cascade),
B4-RUN (confirm carry on real history — network-gated).

---

## Iteration 11 — 2026-06-08 — funding attribution by held size (B9b / C4)

**Context.** Honest-measurement leverage (#3), continuing the B9 thread. Iter 7's
`_agent_funding_payments` split each funding payment *equally* among the agents
holding a coin (from the decision log). But funding is paid **on size**: if agent
A holds 10 units and B holds 1, A should earn ~10× B's funding, not an even half.
Equal-split mis-states every multi-holder funding payment — the exact revenue line
carry strategies (`xfund_carry_v1`, `funding_carry_v1`) live or die on. B9 (Iter
10) gave us fills-based position replay, so we can now attribute by true size.

**Changed (1 commit).**
- **metrics.py** — `_coin_agent_sizes_over_time` replays fills into per-coin
  time-ordered snapshots of each agent's *signed* net size; `_sizes_as_of`
  (bisect) looks up the held book at any funding timestamp. `_agent_funding_payments`
  now splits each payment by `usdc * (agent_signed_size / account_net_size)` — the
  economic decomposition that (a) weights by size, (b) gives a hedging short a
  *negative* (collected) share, (c) sums **exactly** to the account total, (d)
  leaves manual-only size under `_account`. Falls back to the old decision-log
  equal-split only when a coin has no fills yet (paper / pre-fills history), so the
  existing decision-log tests stay valid.
- **tests** — 3 new: size 3:1 split → $3/$1 (not $2/$2); offsetting hedge (long 3
  / short 1, net 2) → +$3 / −$1, summing to the $2 account total; flat-after-close
  gets nothing. Plus `_fill` gained a `side` param for short fills.

**Evidence.** 102 → **105 tests pass**; `ruff check src tests scripts` clean.

**Why it matters.** Per-agent funding (hence net/edge/Sharpe and every gate that
keys off them) now reflects how much each agent actually held, not a head-count.
This is measurement plumbing, not an edge claim — but it's the number the
supervisor will use to promote/demote carry strategies, so it must be true.

**What's next (loop).** B12 (consolidate `runtime.run_tick` vs `femr_tick`, M3),
userFills WS for instant maker-fill detection, B11 (feed/retire liq_cascade),
B4-RUN (confirm carry on real history — network-gated).

---

## Iteration 12 — 2026-06-08 — retire/feed liq_cascade (B11 / C6)

**Context.** Cadence/structure leverage. liq_cascade was the last "dead" agent
(REVIEW C6): `_enrich_view` POSTed `{"type":"liquidations"}` to `/info`, which is
not a real Hyperliquid info endpoint, so the list was always empty. Iter 5 added a
real WS liquidation feed (`trades` channel `liquidation` flag), but two gaps
remained: the phantom REST call still ran every tick, and — critically — the agent
could not tell a *calm market* (no liqs) from a *broken feed* (no source), so in
the default REST-only path it was silently inert and would have traded the instant
any stray data appeared. The risk control looked present but wasn't explicit.

**Changed (1 commit).**
- **cli/main.py `_enrich_view`** — removed the phantom REST liquidations call;
  now sets `liquidations=[]` and `liquidations_feed=False` (REST has no real
  liquidation source). One fewer doomed network round-trip per tick.
- **cli/main.py WS overlay** — a fresh WS snapshot now sets
  `liquidations_feed=True` and always passes through its liqs (empty = calm, not
  broken), so liq_cascade is *enabled* only when genuinely fed.
- **agents/liq_cascade.py** — `decide` gates **entries** on `liquidations_feed`:
  no feed → a single explicit "feed unavailable (set HLBOT_WS_SNAPSHOT) — entries
  disabled" hold. Exits (TP/SL/max-hold) still run unconditionally so a live
  position is never stranded if the feed drops mid-hold. Tightening-only.
- **backtest/engine.py** — synthetic frames set `liquidations_feed=True` (the
  backtest *is* a controlled feed), so liq_cascade remains backtestable.

**Evidence.** 105 → **109 tests pass** (4 new: no-feed suppresses entries,
real-feed enters same side as cascade, real-feed-but-calm holds with n_events=0,
exits run even without a feed); `ruff check src tests scripts` clean; CLI imports.

**Why it matters.** No agent now trades on a dead/ambiguous data source. This is a
safety/honesty tightening (consistent with the risk-reducing-only rule), not an
edge claim — liq_cascade still needs a confirmed edge before any live use, and the
live gate is unchanged. With B11 closed, every remaining "dead/phantom" finding
from the review is resolved.

**What's next (loop).** B12 (consolidate `runtime.run_tick` vs `femr_tick`, M3),
userFills WS for instant maker-fill detection, B4-RUN (confirm carry on real
history — network-gated), B16 (HL vault eval for AUM).

---

## Iteration 13 — 2026-06-08 — extract + test the live order-placement loop (B12 / M3 / D2)

**Context.** Structure/devops leverage. The actual code that places real orders
lived inlined in `cli/main.py::femr_tick` (~70 lines): guardrail/cooldown/
resting-quote gating, maker-vs-taker branching, fill-confirmed logging with the
REAL fill px/sz. REVIEW M3 ("two execution paths; the safe wrapper is dead code
for live") + D2 ("no coverage of the live path") flagged this as untested
risk — and "evidence before capital" means the order path must be tested before
any live use. This is the first atomic slice of B12.

**Changed (1 commit).**
- **`agents/runtime.py`** — new `execute_decisions(conn, exchange, view,
  decisions, *, agent_names, guardrails_ok, execution)` returning an ordered list
  of `ExecEvent(kind, agent, coin, message)`. Behavior is a faithful move of the
  old loop: `place` blocked on guardrail-fail / cooldown / an already-resting
  maker quote; maker mode posts a post-only limit at the near touch (book-aware,
  never crossing) and dedups; taker sends a market order; `place`/`flatten` are
  logged ONLY after exchange acceptance, with the real fill px/sz; a taker reject
  logs a `rejected` row so the coin enters cooldown. Presentation (rich strings)
  rides in `ExecEvent.message` so the CLI stays a thin printer.
- **`cli/main.py`** — `femr_tick` now calls `execute_decisions` and prints each
  event; removed the inlined loop and the now-unused exec imports.

**Evidence.** 109 → **116 tests pass** (7 new in `test_execute_decisions.py`:
real-fill-price-not-pretrade-mid, reject→cooldown, guardrail-block-never-hits-
exchange, cooldown-skip, maker-rests-at-touch + dedup, flatten-logs-real-exit,
foreign-agent/missing-coin ignored — all via a fake exchange + real in-memory DB);
`ruff check src tests scripts` clean; CLI imports/registers. autocommit
(`isolation_level=None`) confirmed, so no commit-semantics drift from the move.

**Why it matters.** The live order path is now unit-tested and lives in one
importable function instead of buried untested CLI code — a prerequisite for
trusting it with capital, and the foundation for finishing B12 (sharing the
view/risk/guardrail preamble between `run_tick` and `femr_tick`). No edge claim;
this is safety/structure plumbing.

**What's next (loop).** Finish B12 (reusable tick harness so `run_tick` ==
live path end-to-end), userFills WS for instant maker-fill detection, B4-RUN
(confirm carry on real history — network-gated), B16 (HL vault eval for AUM).

---

## Iteration 14 — 2026-06-08 — share one decision-gathering path + crash isolation (B12 / M3 / D2)

**Context.** Structure/devops leverage, continuing B12. After Iter 13 extracted
the order-*placement* loop (`execute_decisions`), the order-*gathering* loop was
still duplicated: `runtime.run_tick` (paper `tick` cmd) and `cli.femr_tick` (live)
each had their own "ask each agent to `decide()`, set `is_paper`, log" loop with
divergent policy. Critically, only `run_tick` isolated a crashing agent — in
`femr_tick`, one agent's `decide()` raising would abort the **whole live tick**,
so risk-reducing flattens from healthy agents wouldn't run. That's a real safety
gap (REVIEW M3 "two paths; the safe wrapper is dead code for live" + D2).

**Changed (1 commit).**
- **`agents/runtime.py`** — new `gather_decisions(conn, agents, view, *, is_paper,
  defer_exec_logging=False, log_holds=True, honor_enabled=True)`: one tested
  function owning what gets logged and when. Catches a `decide()` that raises,
  records an `error` row (always paper), and continues to the next agent.
  `defer_exec_logging` returns `place`/`flatten` without logging them (the live
  path logs them only post-fill, with real px/sz, so cooldown never sees intent
  rows); `log_holds=False` drops hold noise; `honor_enabled` skips `enabled=0`
  agents. `run_tick` is now a thin `fetch_market_view` + `gather_decisions(...,
  is_paper=force_paper)` (it places no orders — paper path); its dead
  `force_paper=False` per-agent-mode branch is gone.
- **`cli/main.py` `femr_tick`** — the inlined gather loop is replaced by
  `gather_decisions(..., is_paper=not live, defer_exec_logging=True,
  log_holds=False, honor_enabled=False)`, faithfully preserving its logging policy
  **and** gaining the crash isolation it lacked. Removed the now-unused
  `log_decision` import.

**Evidence.** 116 → **120 tests pass** (4 new in `test_gather_decisions.py`:
paper policy logs holds+place immediately; live policy defers place/flatten and
skips holds while still returning them; a crashing agent is isolated as an `error`
row so a healthy agent's flatten survives; `honor_enabled` skips disabled agents
and is bypassed on the femr path); `ruff check src tests scripts` clean; CLI
imports/registers.

**Why it matters.** The live decision path now shares one importable, tested
function with the paper path instead of a hand-rolled loop, and a single bad agent
can no longer take down a live tick — flattens (risk reduction) still execute.
Safety/structure plumbing, not an edge claim.

**What's next (loop).** Finish B12 (fold the femr_tick clearinghouse/position/
reconcile/allocator preamble into a reusable harness so `run_tick` == live path
end-to-end), userFills WS for instant maker-fill detection, B4-RUN (confirm carry
on real history — network-gated), B16 (HL vault eval for AUM).

---

## Iteration 15 — 2026-06-08 — extract + test the live tick preamble (B12 / M3 / D2)

**Context.** Structure/devops leverage, continuing B12. Iters 13–14 pulled the
order-placement (`execute_decisions`) and decision-gathering (`gather_decisions`)
loops out of `cli.femr_tick` into tested `runtime` functions. The remaining
duplicated/untested live-only code was the **preamble**: parsing HL
`clearinghouseState` into the bot's position-dict shape and the per-agent
stale-ownership reconcile loop. Both were inlined in `femr_tick` with zero
coverage (REVIEW M3 "two paths; the safe wrapper is dead code for live" + D2 "no
coverage of the live path") — and "evidence before capital" means the live
position/reconcile plumbing must be tested before it gates real orders.

**Changed (1 commit).**
- **`agents/runtime.py`** — two new pure-ish, importable functions:
  - `positions_from_clearinghouse(st)` — faithful move of the inlined
    `assetPositions[].position` parse into the normalized list of dicts (coin,
    szi, entry_px, position_value, unrealized_pnl, liquidation_px, leverage,
    margin_used). Malformed entries skipped, not aborting the tick.
  - `reconcile_agents(conn, all_positions, agent_names)` — runs
    `reconcile_positions` per agent (name-match ownership, so they must reconcile
    independently) and returns only agents that had stale coins cleared. Local
    import of `exec.orders.reconcile_positions` to avoid a circular import (same
    pattern `execute_decisions` uses).
- **`cli/main.py` `femr_tick`** — the ~25-line inlined position-build + reconcile
  loop is replaced by `positions_from_clearinghouse(st)` +
  `reconcile_agents(conn, all_positions, [a.name for a in agents])`. Imports
  updated (added the two runtime fns; dropped the now-unused `reconcile_positions`
  from the `exec.orders` import). Behavior preserved exactly.

**Evidence.** 120 → **124 tests pass** (4 new in `test_tick_harness.py`: field
parse incl. nested leverage; empty/missing/None-safe defaults; reconcile clears
exactly the stale agent's coin and leaves the live one owned; no-op when all
present); `ruff check src tests scripts` clean; `from hl_bot.cli.main import app,
femr_tick` imports OK.

**Also (bookkeeping).** Ticked **B13/M6** done — verified both `exec/orders.py`
and `scripts/daily_scorecard.py` already resolve the trader address from
`HL_TRADER_ADDRESS`/`HL_ADDRESS` env (legacy default only as fallback); the P2
`[ ]` was stale since Iteration 6.

**Why it matters.** The live position-parsing and reconcile plumbing is now unit-
tested and lives in importable `runtime` functions instead of buried untested CLI
code — another prerequisite for trusting the live path with capital, and the next
slice of B12 toward `run_tick == femr_tick` end-to-end. Safety/structure plumbing,
not an edge claim.

**What's next (loop).** Finish B12 (fold the remaining femr_tick preamble —
clearinghouse fetch → risk-cap → allocator caps → view enrich/WS overlay — into a
reusable harness), userFills WS for instant maker-fill detection, B4-RUN (confirm
carry on real history — network-gated), B16 (HL vault eval for AUM).

---

## Iteration 16 — 2026-06-08 — extract + test the allocator cap resolution (B12 / M3 / D2)

**Context.** Structure/devops leverage, continuing B12. Iters 13–15 pulled the
order-placement (`execute_decisions`), decision-gathering (`gather_decisions`),
and clearinghouse-parse/reconcile (`positions_from_clearinghouse`/`reconcile_agents`)
loops out of `cli.femr_tick` into tested `runtime` functions. The next inlined,
untested preamble block was the **allocator cap resolution**: build a
`MetaAllocator` from the 5x/1x risk caps, `allocate()` the 7d-performance split,
`resolve_agent_caps()` the layered rule, then mutate each agent's `cfg` with the
binding total/per-trade caps. ~30 lines of risk-critical logic with zero direct
coverage (REVIEW M3 "two paths; the safe wrapper is dead code for live" + D2 "no
coverage of the live path") — and "evidence before capital" means the code that
decides how much each agent may risk must be tested before it gates real orders.

**Changed (1 commit).**
- **`agents/runtime.py`** — new `apply_allocator_caps(conn, agents, risk_cap) ->
  AllocatorCaps`: a verbatim move of the inlined allocator→resolve→apply loop.
  Returns `AllocatorCaps(allocs, effective_caps, effective_order_caps)` and
  mutates each agent's `cfg` in place exactly as before (the layered rule is
  unchanged: configured sub-legacy cap wins, legacy blanket ceilings replaced by
  the dynamic 1x cap, configured per-trade sizes never raised; agents without a
  `cfg` keep their raw alloc and are untouched). `MetaAllocator`/`resolve_agent_caps`
  are imported locally inside the fn (same pattern the other runtime helpers use).
- **`cli/main.py` `femr_tick`** — the ~30-line inlined block is replaced by
  `caps = apply_allocator_caps(conn, agents, risk_cap)` + unpacking; the console
  print of caps is unchanged. Added `apply_allocator_caps` to the runtime import;
  dropped the now-dead module-level `MetaAllocator`/`MetaAllocatorConfig` and
  `resolve_agent_caps` imports (ruff-verified unused).

**Evidence.** 124 → **127 tests pass** (3 new in `test_tick_harness.py`: explicit
sub-legacy cfg cap honored + per-trade preserved + cfg mutated in place; no
configured cap → dynamic 1x per-position ceiling binds; agent without a `cfg`
keeps its raw alloc and is left untouched); `ruff check src tests scripts` clean;
`from hl_bot.cli.main import app, femr_tick` imports OK.

**Why it matters.** The risk-sizing path — how much notional each agent is
allowed — now lives in one importable, unit-tested function instead of buried
untested CLI code, another prerequisite for trusting the live path with capital
and the next slice of B12 toward `run_tick == femr_tick` end-to-end. Tightening-
only by construction (no caps raised); safety/structure plumbing, not an edge claim.

**What's next (loop).** Finish B12 (fold the remaining femr_tick preamble —
clearinghouse fetch → risk-cap → view enrich/WS overlay — into a reusable harness
so `run_tick` == live path end-to-end), userFills WS for instant maker-fill
detection, B4-RUN (confirm carry on real history — network-gated), B16 (HL vault
eval for AUM).

---

## Iteration 17 — 2026-06-08 — extract + test the WS snapshot overlay (B12 / M3 / D2)

**Context.** Structure/devops leverage, continuing B12. Iters 13–16 pulled the
order-placement (`execute_decisions`), decision-gathering (`gather_decisions`),
clearinghouse-parse/reconcile (`positions_from_clearinghouse`/`reconcile_agents`),
and allocator-cap (`apply_allocator_caps`) loops out of `cli.femr_tick` into
tested `runtime` functions. The next inlined, untested preamble block was the
**WS snapshot overlay**: merge a fresh WS snapshot's mids/funding/book_top onto
the live REST view and flip on the real liquidations feed for liq_cascade. ~18
lines with zero direct coverage (REVIEW M3 "two paths; the safe wrapper is dead
code for live" + D2 "no coverage of the live path"). The liquidations-feed
semantics in particular ("a fresh-but-empty snapshot is a calm market, NOT a
broken feed, so still enable entries") are subtle and were untested.

**Changed (1 commit).**
- **`agents/runtime.py`** — new `overlay_ws_snapshot(view, snap) -> WsOverlay`: a
  pure, filesystem-free move of the inlined additive merge. Mutates `view` in
  place (fresh snapshot mids/funding override; `book_top` merged; `liquidations`
  set and `liquidations_feed=True` whenever a snapshot exists). Returns
  `WsOverlay(applied, n_mids, n_liqs)` so the CLI can print its one-line summary.
  `snap is None` → `applied=False`, view untouched (REST stays the source).
- **`cli/main.py` `femr_tick`** — the ~18-line inlined block is replaced by
  `ov = overlay_ws_snapshot(view, load_fresh_snapshot(...))` + a guarded print.
  The CLI keeps only the `HLBOT_WS_SNAPSHOT` env-read + file load (IO) and the
  console output. Added `overlay_ws_snapshot` to the runtime import.

**Evidence.** 127 → **130 tests pass** (3 new in `test_tick_harness.py`: None is
a no-op that leaves REST untouched and injects no feed flag; a real snapshot
overrides BTC mid while preserving ETH, merges book_top, and enables the feed
with both liqs; a fresh-but-empty snapshot still sets `liquidations_feed=True`
with `liquidations=[]`); `ruff check src tests scripts` clean; `from
hl_bot.cli.main import app, femr_tick` imports OK.

**Why it matters.** The live WS-overlay path — including the subtle
"empty-feed-is-still-a-feed" rule that gates liq_cascade entries — now lives in
one importable, unit-tested function instead of buried untested CLI code. Another
prerequisite for trusting the live path with capital and the next slice of B12
toward `run_tick == femr_tick` end-to-end. Purely additive merge; safety/structure
plumbing, not an edge claim.

**What's next (loop).** Finish B12 (fold the remaining femr_tick preamble —
clearinghouse fetch → risk-cap → view enrich — into a reusable harness so
`run_tick` == live path end-to-end), userFills WS for instant maker-fill
detection, B4-RUN (confirm carry on real history — network-gated), B16 (HL vault
eval for AUM).

---

## Iteration 18 — 2026-06-08 — extract + test the bot-owned/manual partition (B12 / M3 / D2)

**Context.** Structure/devops leverage, continuing B12. Iters 13–17 pulled the
order-placement, decision-gathering, clearinghouse-parse/reconcile, allocator-cap,
and WS-overlay loops out of `cli.femr_tick` into tested `runtime` functions. The
next inlined, untested preamble block was the **bot-owned vs manual position
partition**: union each roster agent's `bot_owned_coins` and split every live HL
position into bot-owned vs manual. ~6 lines with zero direct coverage (REVIEW M3
"two paths; the safe wrapper is dead code for live" + D2 "no coverage of the live
path"). This is risk-relevant: `manual_coins` is the set of positions the bot must
NOT touch (opened by hand or by a filtered-out, not-promoted agent), so the
classification must be correct before the live path acts on it.

**Changed (1 commit).**
- **`agents/runtime.py`** — new `classify_position_ownership(conn, all_positions,
  agent_names) -> PositionOwnership(owned_by_agent, owned_all, manual_coins)`: a
  faithful move of the inlined per-agent `bot_owned_coins` union + manual split.
  Ownership keys off each agent's CONFIRMED place/flatten decision log; a coin
  owned by an agent NOT in `agent_names` (e.g. a not-promoted live agent) correctly
  falls into `manual_coins`; `manual_coins` preserves `all_positions` order so the
  CLI display is unchanged. `bot_owned_coins` imported locally (same pattern the
  other runtime helpers use).
- **`cli/main.py` `femr_tick`** — the inlined `owned_all` loop + `manual_coins`
  partition is replaced by `classify_position_ownership(...)` + unpacking. The
  femr-specific `live_positions` line (still derived from `bot_owned_coins(femr_v1)`
  for the FEMR view) and the console display are unchanged. Added
  `classify_position_ownership` to the runtime import.

**Evidence.** 130 → **133 tests pass** (3 new in `test_tick_harness.py`:
two-agent split with a manual SOL; a filtered-out agent's coin shows as manual
with order preserved; place-then-flatten drops ownership → manual); `ruff check
src tests scripts` clean; `from hl_bot.cli.main import app, femr_tick` imports OK.

**Why it matters.** The live bot-owned/manual classification — which gates what the
bot considers its own vs hands-off — now lives in one importable, unit-tested
function instead of buried untested CLI code. Another prerequisite for trusting the
live path with capital and the next slice of B12 toward `run_tick == femr_tick`
end-to-end. Behavior-preserving; safety/structure plumbing, not an edge claim.

**What's next (loop).** Finish B12 (fold the remaining femr_tick preamble —
clearinghouse fetch → risk-cap → view enrich — into a reusable harness so
`run_tick` == live path end-to-end), userFills WS for instant maker-fill
detection, B4-RUN (confirm carry on real history — network-gated), B16 (HL vault
eval for AUM).

---

## Iteration 19 — 2026-06-08 — userFills WS for instant maker-fill detection (C1/C7)

**Context.** Stepped off the B12 refactor treadmill (iters 13–18 were all P2
structure slices) back onto the P0 leverage axis: **kill the taker tax via maker
execution**. The maker lifecycle (B2b) was built but its fill detection only read
the `fills` table, which is populated by REST `ingest_fills`. On a 5-min cron that
means a post-only quote can fill and sit *unreconciled for a full tick* — the
position isn't marked owned and stops/TPs aren't keyed to the real fill — exactly
the C7 "signal horizon ≫ action cadence" failure, but for our own fills. The fix
is the long-deferred "userFills WS" task: capture our fills continuously over the
socket so the tick reconciles them immediately. B1/B4-RUN stay network-blocked
(no HL history reachable; no cache present), so this was the highest-leverage
*unblocked* item.

**Changed (1 commit).**
- **`ingest/hyperliquid.py`** — extracted `upsert_fill(conn, f) -> int` (single
  raw-fill attribution + INSERT-OR-IGNORE) from `ingest_fills`, which now just
  sums it. New `ingest_ws_user_fills(conn, fills)` upserts WS-captured fills
  through the same path, so a fill seen over WS then REST dedups by (hash,tid).
- **`ingest/ws.py`** — `MarketState` gained a `user_fills` deque; `apply_message`
  handles the `userFills` channel (`data.fills`, raw dicts kept verbatim, drops
  entries missing hash/tid); `recent_user_fills(window_s=1800)` + inclusion in
  `to_snapshot()`; `load_fresh_snapshot` threads them into `extra["user_fills"]`;
  `run_ws(user_address=)` subscribes to `{"type":"userFills","user":...}`.
- **`agents/runtime.py`** — `overlay_ws_snapshot` now carries `user_fills` from the
  snapshot onto the live view (additive, same as the liquidations feed).
- **`cli/main.py` `femr_tick`** — in the maker prep, after REST `ingest_fills`,
  `ingest_ws_user_fills(conn, view.extra["user_fills"])` runs before
  `reconcile_maker_fills`, so WS fills are reconciled in the same tick.

**Evidence.** 133 → **136 tests pass** (3 new: WS userFills captured + windowed +
malformed dropped; snapshot round-trip preserves cloid; end-to-end — a WS fill
upserted via `ingest_ws_user_fills` flips a resting maker order to owned via
`reconcile_maker_fills` with the REAL fill px, and a re-seen fill dedups to 0);
`ruff check src tests scripts` clean; `from hl_bot.cli.main import app, femr_tick`
imports OK.

**Why it matters.** Maker execution is the #1 structural fix for the bleed (REVIEW
C1), but it's only viable if fills are detected promptly — otherwise a filled quote
is an unmanaged position until the next cron. This closes that loop on the free WS
feed, deduped against REST so nothing double-counts. Plumbing/safety toward a
positive *live* maker edge; not itself an edge claim (still gated, still paper).

**What's next (loop).** Finish B12 (fold the remaining femr_tick preamble into a
reusable harness so `run_tick` == live path end-to-end), B4-RUN (confirm carry on
real history — network-gated), B16 (HL vault eval for AUM).

---

## Iteration 20 — 2026-06-08 — fix funding-history truncation; B1 taker-tax + B4-RUN carry confirm on real history

**Context.** Network to `api.hyperliquid.xyz` is reachable on this host (200 on a
`meta` probe), so the long-blocked P0 edge work — B1 (quantify the taker tax) and
B4-RUN (confirm carry on real history) — was finally unblocked. This is the whole
point of the mission, so it took priority over the P2 B12 refactor treadmill.

**B1 — taker tax (90d 1h, BTC/ETH/SOL/HYPE).** Real history, `--compare`:
- `twap_mr_v1`: taker net −$128.67 / **−10.0bps** → maker −$60.74 / **−4.6bps**
  (taker tax ≈ **5.4bps**; both still negative, 646 trades, ~47% win).
- `twap_mr_regime_v1`: taker −9.8bps → maker −4.5bps. The regime filter is
  **nearly inert at 1h on majors** — barely differs from baseline twap_mr.
- `femr_v1`: **0 trades** on majors — funding never reaches the 1.5bps/hr enter
  threshold (majors' |funding| absmax ~0.6bps/hr).

**Diagnosis of the 0-trade carry/femr runs (the real find).** femr + both carry
agents made **0 trades** over 90d even after adding liquid high-funding alts
(ZEC, ADA, TRX, …). Root cause: **`fetch_funding_history` returned exactly 500
rows** — HL's per-call page cap — covering only the *oldest* ~20.8 days of any 90d
request (e.g. ZEC 2026-03-11→04-01); the recent ~69 days (incl. ZEC's current
~1.6bps/hr funding spike) were silently dropped and then **carried forward as a
stale constant** by `funding_rate_at`. So *every carry/FEMR backtest over >20d was
invalid* — they never saw the funding regimes that make carry trade at all.

**Fix (1 commit).**
- **`backtest/data.py`** — new pure `paginate_by_time(page_fn, start, end, …)` that
  walks a time-ordered, page-capped HL endpoint forward (re-request from last
  row's `time`+1ms until a short page / window end / no-progress), de-duping by
  time and returning sorted-ascending. `fetch_funding_history` now fetches via it.
  Validated live: ZEC 90d now returns **2160 rows** spanning the full 90d, absmax
  **2.67bps/hr**, 16 bars ≥1.5bps (was 500 rows / 20.8d / 0.54bps absmax).
- **`tests/test_backtest.py`** — 2 unit tests: paginator clears a 500-row cap over
  1200 rows in ≥3 pages (sorted, deduped); and stops after one page when the
  endpoint ignores `startTime` (no-progress guard, no infinite loop).

**B4-RUN — carry on corrected funding (90d 1h, 10 liquid coins incl. ZEC).** Now
they trade, and the honest verdict is **still no edge**:
- `xfund_carry_v1` (market-neutral cross-sectional): maker **−4.3bps** / taker
  −9.8bps, 62 trades, maxDD −0.2%. `hlbot confirm --prefer maker` → **NOT
  CONFIRMED**, but OOS-maker was faintly **+3.4bps / +0.60 Sharpe (52 trades)**
  vs in-sample −43.7bps — a hint the *recent* funding regime (now captured) carries
  a small market-neutral signal. Closest thing to break-even in the book.
- `funding_carry_v1` (single-name hold-to-collect): maker **−111bps**, 20 trades,
  30% win — price moves on volatile alts dwarf the funding collected. Worst.
- `femr_v1`: maker −27.9bps, 32 trades.

**Evidence.** 136 → **138 tests pass**; `ruff check src tests scripts` clean;
caches stay gitignored (`data/`). Backtest tables above are reproducible via
`hlbot backtest --agent <a> --coins <U> --days 90 --compare`.

**Why it matters.** The carry/FEMR backtests were *silently wrong* — measuring the
wrong 20 days and pretending it was 90 — so any prior "no edge" or "edge" claim on
funding strategies was unfounded. With funding now fetched correctly, measurement
is trustworthy and the answer is honest: no positive net-of-cost edge yet, with
**market-neutral xfund-maker the only near-break-even candidate**. Not an edge
claim; nothing promoted; everything still paper/gated.

**What's next (loop).** B1c (edge hunt on corrected funding: rank a wider universe
by historical |funding|×liquidity, longer hold / tighter entry / funding-decile
cross-section for xfund), B1b (paginate `fetch_candles` for fine-interval long
windows — same cap class), then resume B12 consolidation.

---

## Iteration 21 — 2026-06-08 — paginate candle fetch (B1b)

**Context.** Iter 20 found and fixed HL's silent per-call row cap on
`fundingHistory` (500 rows → only the oldest ~20d of a 90d window survived,
invalidating every >20d carry backtest). The candle endpoint shares the same
failure class: `fetch_candles` issued a single `candleSnapshot` call, and HL caps
that at ~5000 rows. 90d×1h = 2160 bars is safe, but the edge hunt (B1c) wants
*fine* intervals (1m/5m) over long windows — 30d×5m = 8640, 30d×1m = 43200 — which
would have silently dropped the most recent bars and produced wrong-but-plausible
backtests, exactly the trap that made the funding results untrustworthy. This is
the highest-leverage *unblocked* item (network-independent correctness fix that
de-risks the upcoming fine-interval edge hunt).

**Changed (1 commit).**
- **`backtest/data.py`** — `fetch_candles` now fetches through the existing
  `paginate_by_time` walker (added in Iter 20), keyed on the candle open-time `t`
  with `page_limit=CANDLE_PAGE_LIMIT` (new module constant = 5000). Same forward-
  walk + de-dup + sort-ascending + no-progress guard the funding fetch uses, so
  long fine-interval windows recover all bars instead of truncating to the newest
  ~5000. The single-call body became a `_page(s, e)` closure (mirrors
  `fetch_funding_history`). Returns oldest-first (was "newest-last" — the previous
  docstring was already wrong; HL returns oldest-first and the paginator sorts
  ascending, which is what `build_frames` already assumes via its `sorted(...,t)`).
- **`tests/test_backtest.py`** — `test_fetch_candles_paginates_past_the_row_cap`:
  monkeypatches `httpx.Client` with a fake capped endpoint and shrinks
  `CANDLE_PAGE_LIMIT` to 3, proving 10 candles are recovered across ≥3 pages,
  sorted+deduped, keyed on `t`. Offline, no network.

**Evidence.** 138 → **139 tests pass**; `ruff check src tests scripts` clean.

**Why it matters.** B1c (the edge hunt) will lean on fine-interval long windows;
without this fix those backtests would have been silently wrong in the recent bars
— the same trap that invalidated the carry results before Iter 20. Pure
correctness/measurement plumbing; not an edge claim, nothing promoted.

**What's next (loop).** B1c (edge hunt on corrected funding + now-correct fine
candles: rank a wider universe by historical |funding|×liquidity, longer hold /
tighter entry / funding-decile cross-section for xfund — network-gated), then
resume B12 consolidation (fold remaining `femr_tick` preamble into a shared
harness).

## Iteration 22 — 2026-06-08 — `--config` sweep tool + B1c edge hunt prunes carry hypotheses

**Context.** Network reachable (HL `meta` → 200) and the corrected-funding cache is
present, so B1c (the edge hunt on xfund_carry, the closest-to-break-even candidate)
was the top unblocked P0. Blocker found first: `backtest`/`confirm` built every
agent with a hardcoded `config={}`, so **no parameter sweep was possible from the
CLI** — you couldn't test "tighter entry" or "longer hold" without editing code.

**Changed (1 commit).**
- **`cli/main.py`** — new pure `parse_agent_config(s)` (empty→`{}`; must decode to a
  JSON *object*; array/scalar/malformed is a hard error so a typo can't silently run
  defaults and mislabel a sweep) and `_backtest_factories(cfg)` (one factory map with
  the override baked in, replacing the duplicated dict in both commands). `backtest`
  and `confirm` gained a `--config '{json}'` flag; the backtest table title echoes
  `cfg=…` so a sweep result is self-documenting.
- **`tests/test_backtest.py`** — `test_parse_agent_config_and_factory_override`:
  empty/whitespace→defaults; valid object parses; `[1,2]`/`5`/`"x"`/`{not json}` all
  raise; and the override actually reaches the constructed agent
  (`enter_funding_per_hr` 0.0001→0.0003) while an un-overridden agent keeps defaults.

**Evidence (tests/lint).** 139 → **140 tests pass**; `ruff check src tests scripts`
clean. Caches stay gitignored (`git status` shows only the 2 source files).

**B1c edge hunt (xfund_carry_v1, 90d 1h, maker numbers; reproducible via the new
`--config`).**
- *Baseline 10-coin* (ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,TRX,ZEC): maker −4.3bps,
  62 trades, 48% win, Sharpe −0.45. (Matches Iter 20.)
- *Tighter entry* `enter=0.0002`: maker **−40.5bps**, 18 trades, 33% win, Sharpe
  −3.17 — much WORSE. `enter≥0.0003`: **0 trades** (per-hr funding caps ~2.7bp/hr on
  this universe, so the threshold just starves the book).
- *`top_k` 1/4/6, bigger book*: ~inert vs baseline (eligible legs/side cap out;
  notional wasn't binding).
- *Wider 20-coin* (added SUI,APT,INJ,TIA,SEI,LTC,ARB,OP,WIF,NEAR — cached): maker
  **−12.1bps**, 154 trades, 42% win, Sharpe −2.39 — clearly WORSE than 10-coin.
  `hlbot confirm --prefer maker` → **NOT CONFIRMED**, OOS-maker **−23.6bps / −5.13
  Sharpe** (no faint-positive OOS like Iter 20's narrow run). `top_k=4/6` inert.

**Why it matters (the finding).** Both naive B1c directions are now empirically
**pruned as wrong**: tightening entry AND widening the universe each make xfund carry
*worse*, because high-|funding| alts carry extreme funding *because* they're
volatile/squeezing — the residual price variance of the cross-sectional book buries
the few-bp/hr funding spread whether you concentrate or diversify. The Iter-20
"OOS-maker +3.4bps" was a narrow-10-coin sample artifact, not a robust edge. The
single best config in the book remains the loosest, most-diversified baseline at
**−4.3bps maker — still negative.** No edge; nothing promoted; all paper/gated.

**What's next (loop).** Continue B1c on the *un-pruned* levers: (a) longer max-hold /
kill the rotation churn (154 trades over 90d is a lot of cross/fee for a "hold to
collect" thesis), (b) beta-neutralise the cross-section (dollar-neutral ≠ market-
neutral when the short legs are higher-beta alts), (c) a lower cadence where funding
accrual outweighs per-bar price noise. Then resume B12 consolidation.

---

## Iteration 23 — 2026-06-08 — B1c: cut-the-churn lever pruned (hold_while_eligible WORSE)

**Context.** Network reachable (HL `meta` → 200) and the corrected-funding caches
are present (10- and 20-coin 90d 1h), so B1c — the edge hunt on xfund_carry, still
the closest-to-break-even candidate — was the top unblocked P0. Iter 22 left three
un-pruned levers; took the most clearly-motivated one: **(a) cut the rotation
churn.** The Iter-22 20-coin run did 154 trades over 90d, a lot of cross/fee for a
"hold to collect" thesis, and the churn's source is the `want is None` →
"DROPPED from carry set" exit: a leg still carrying strong funding gets closed
merely because it fell out of the top-K *rank* this hour, then often re-enters next.

**Changed (1 commit).**
- **`agents/xfund_carry.py`** — new config `hold_while_eligible: bool = False`
  (default off → behavior/CI unchanged). When on, a held leg is kept as long as its
  funding stays eligible (`|rate| ≥ exit_funding_per_hr`) and on the correct side,
  decoupling exits from rank rotation. Refactored the exit ladder to check the
  funding *sign* directly (new `_funding_side`) so a flip is caught even for a leg
  outside the current top-K (a correctness improvement that also makes the held-leg
  flip-exit work); rank-rotation exit is now gated behind `not hold_while_eligible`.
- **`tests/test_funding_carry.py`** — 2 new: a rank-rotated-but-still-eligible held
  leg is flattened in default mode and KEPT (and not re-entered) under
  `hold_while_eligible`; and even in hold mode a held leg still exits on
  funding-flip and funding-normalize.

**Evidence (tests/lint).** 140 → **142 tests pass**; `ruff check src tests scripts`
clean. Caches stay gitignored.

**B1c result (xfund_carry_v1, 90d 1h, 10-coin ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,
TRX,ZEC; reproducible via `--config`).**
- *Baseline* (rank-rotation exits): maker **−4.3bps** / −0.66 net, 62 trades, 48%
  win, Sharpe −0.45. (Matches Iter 20/22.)
- *`hold_while_eligible=true`*: maker **−17.6bps** / −1.57 net, **36 trades** (churn
  cut as designed), 44% win, Sharpe −0.82 — clearly **WORSE**.

**Why it matters (the finding).** Lever (a) is **pruned**: cutting the churn does
NOT help — it hurts. The hypothesis "154 trades is wasteful turnover" is falsified.
The rotation churn isn't waste: it concentrates the book into the *highest*-funding
names each hour. Holding a rank-rotated leg means collecting a lower-funding carry
(e.g. the +0.0005/hr coin instead of rotating to +0.0010/hr) while *still* eating
that volatile alt's price variance over a longer hold — strictly less carry per
unit of price risk. The fee saved (26 fewer trades) is dwarfed by the carry given
up. The single best config in the book remains the loosest baseline at **−4.3bps
maker — still negative.** No edge; nothing promoted; all paper/gated. The config
lever is kept (default off, tested) so this dead end isn't re-explored.

**What's next (loop).** The remaining un-pruned B1c levers: (b) beta-neutralise the
cross-section (dollar-neutral ≠ market-neutral when the short legs are higher-beta
alts — likely the real reason price variance buries the carry; needs rolling-beta
sizing, available in backtest via the `closes` series), or (c) a lower cadence
(4h/1d) where funding accrual outweighs per-bar price noise. Then resume B12
consolidation.

---

## Iteration 24 — 2026-06-09 — strategic replan: capital formation

**Context.** Bot live + profitable on twap_mr_v1 (+29.5bps, ~7.9 Sharpe). The loop's
B1/B4-RUN (iters 20–23) confirmed carry strategies on REAL history and **pruned them**
(no durable edge — xfund/funding carry + the hold-while-eligible lever all failed the
gate). So twap_mr_v1 remains the one proven engine. User can commit ~$10k personal and
wants vault-AUM + prop-funded + a moonshot, treating "$1M by year-end" as a direction.

**Changed (docs/planning).**
- **docs/CAPITAL.md** — capital-formation playbook: required-monthly-return math
  (capital is the lever), HL vault economics (10% share / ≥5% leader TVL / API-compatible,
  fees to verify), prop firms (Hypernova/Propr/Velotrade), SMA bar, moonshot sleeve, and
  sequencing (lead with vault, prop parallel, moonshot tiny).
- **docs/ROADMAP_TO_1M.md** — status refresh (twap_mr_v1 proven; carry pruned; deployed
  live; realistic ~$350k–$700k year-end; $1M ~H1-2027; pointer to CAPITAL.md).
- **ralph/BACKLOG.md** — P0.5 (scale twap_mr_v1, widen universe, track-record chart) +
  P3 capital items (B16 researched, B16b vault launch, B-PROP eval prep, B17 moonshot).

**The plan in one line.** Build a 60–90d on-chain track record (Sharpe>1.5, DD<10%) →
unlock vault AUM + prop funding; capital, not heroic returns, is the path to $1M.

**What's next (loop).** Scale twap_mr_v1 with the risk machinery; keep hunting new edge
(carry is pruned); B15c track-record chart for vault/SMA pitching.

---

## Iteration 25 — 2026-06-09 — strategy review + improvement levers + track-record chart

**Context.** User asked for a full strategy review + improvements, plus the B15c chart.
Carry is pruned, so `twap_mr_v1` is the one proven engine — review centers on it.

**Changed (1 commit).**
- **docs/STRATEGY_REVIEW.md** — full review: why twap_mr works (maker-sensitive MR edge,
  real but thin), its structural weaknesses (fades trends, flat sizing, eager exit, no
  funding awareness, short VWAP window), the carry-pruning negative result, the
  multi-agent posture (run only the proven agent), and a prioritized experiment program.
- **twap_mr.py — two opt-in levers (default OFF, proven baseline unchanged):**
  `regime_filter` (drop fades into a strong trend — the #1 structural loss) and
  `size_by_signal` (scale capital by signal strength). `_signal_size_mult` helper.
  Tested (`tests/test_twap_levers.py`). These are experiments the loop A/Bs on real data.
- **reports/track_record.py — `to_html` (B15c):** self-contained HTML page with an inline
  SVG equity curve + per-agent table; `export` now also writes `track_record.html`;
  `hlbot track-record` surfaces it. The shareable artifact for vault/SMA due-diligence.

**Evidence.** **148 tests pass**; ruff clean; HTML export smoke-tested (1.3KB page).

**What's next (loop).** B-REGIME / B-SIZE / B-EXIT — A/B the new twap_mr levers + sweep
exits on real history; B-EDGE2 — hunt a second low-correlation edge (carry is out).

---

## Iteration 26 — 2026-06-09 — run the loop on an always-on box (off the Mac)

**Context.** User's Mac isn't always online; move the self-improvement loop to the
AWS box / a VPS, isolated from the live trading dir.

**Changed (1 commit).**
- **deploy/setup-loop.sh** — one-command setup: clones a SEPARATE workspace
  (/opt/hl-bot-loop, chmod 700), installs uv + Claude Code CLI, configures a
  token-auth push remote, syncs deps, installs the unit, runs a baseline check, and
  prints the two manual steps (claude setup-token OAuth + enable). The agent never
  touches the live /opt/hl-bot; the live bot only auto-deploys the clean commits it pushes.
- **hlbot-loop.service** — repointed to /opt/hl-bot-loop with CLAUDE_USE_OAUTH=1,
  CLAUDE_BIN, an explicit PATH (so the service finds claude/uv), --effort xhigh,
  Restart=always.
- **deploy/README** — "Self-improvement loop (always-on)" section.

**Evidence.** 148 tests pass; ruff clean; shell scripts `bash -n` clean.

**Note.** The loop pushes to GitHub; review its work via the branch + this journal in
any session (no standing SSH needed — the loop IS Claude running autonomously there).

---

## Iteration 27 — 2026-06-12 — B-REGIME A/B on real data + linear build_frames + HL retention finding

**Context.** B-REGIME was the top unblocked P0.6 item: the `regime_filter` lever
(Iter 25, default OFF) needed real-data validation. Network up; fresh 90d 1h cache
for the 10-coin universe (ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,TRX,ZEC).

**B-REGIME result (twap_mr_v1, 90d 1h, 10 coins, reproducible via `--config`).**

| config (move/consistency)        | maker edge | trades | maker Sharpe | maxDD  |
|----------------------------------|-----------:|-------:|-------------:|-------:|
| baseline (no filter)             |    −5.0bps |   1402 |        −2.50 | −21.7% |
| defaults 0.03/0.65               |    −4.7bps |   1394 |        −2.37 | −20.6% |
| 0.03/0.55                        |    −4.4bps |   1084 |        −2.33 | −14.0% |
| **0.015/0.55 (best)**            | **−3.0bps**|    936 |        −1.37 |  −9.9% |
| 0.02/0.55                        |    −3.6bps |    980 |        −1.76 | −11.3% |
| 0.01/0.55                        |    −3.5bps |    902 |        −1.57 | −10.5% |
| 0.05/0.52                        |    −5.0bps |   1140 |        −2.83 | −15.1% |

Walk-forward (`hlbot confirm`, prefer=maker): baseline OOS −7.3bps / best config
OOS −6.0bps — both **FAIL G0**. Verdict: clear dose-response (blocking trend-fades
helps a fader: edge up, DD halved, the peak is interior at 0.015/0.55 so it's not
"block everything"), the lever's *direction* is validated — but it never flips the
sign at 1h cadence. Shipped defaults (0.03/0.65) are inert at 1h (65% hourly-step
consistency almost never fires; only ~8/1390 trades blocked). **Default stays OFF;
no live change** (live twap_mr is +29.5bps — don't perturb it on a proxy backtest).

**The structural insight.** The 1h backtest fades a 60×1h (60-hour) VWAP; the live
bot fades a 60×1m (1-hour) VWAP at ~1m cadence. They are different strategies —
which reconciles "backtest −5bps" with "live +29.5bps". Levers must be validated
at live-like cadence to inform live config.

**Changed (1 commit).**
- **`backtest/data.py` — linear-time `build_frames`** (was the blocker for fine
  cadence): per-frame `upto` prefix scans, full-prefix `closes[:cut]` copies,
  per-frame full `fundingHistory` sweeps, and the per-frame 1440-bar volume sum
  made it O(n²); replaced with per-coin bar/funding cursors (timestamps visit in
  order) + a volume prefix-sum. Funding rows are now sorted by time once
  (implements the documented "most recent ≤ ts" even for unsorted input).
- **`tests/test_backtest.py` — 3 new:** equivalence vs the *verbatim* old logic on
  irregular data (gaps, zero-close candle, malformed funding row, non-default
  window/warmup/bar_hours); unsorted-funding semantics; 20k×1m×2-coin scale smoke.

**Evidence.** **151 tests pass** (was 148); ruff clean. Real-data timing: 90d 5m
BTC+ETH → build **0.16s** (previously quadratic, ~minutes).

**New finding (changes the plan): HL candle retention ≈ 5000 bars/interval.**
Measured: 1m → 3.5d, 5m → 17.4d, 15m → 52.1d, 1h → full 90d. So months of
live-cadence history **cannot be fetched after the fact** — it must be harvested
continuously before it expires. Filed **B-HIST** (rolling 1m/5m candle
accumulator — every day un-deployed is 1m history lost) and **B-CAD** (run the
lever A/Bs at 15m/52d and 5m/17d now; `--vwap-window` CLI + cache-key fix needed —
cached frames bake the window in, key must include it when ≠60).

**What's next (loop).** B-HIST (highest leverage: starts the irreplaceable data
clock), then B-CAD A/Bs of regime_filter + size_by_signal at live-like cadence.

---

## Iteration 28 — 2026-06-12 — B-HIST: rolling fine-candle store + hourly harvest timer

**Context.** Top unblocked item after Iter 27's finding that HL retains only
~5000 candles/interval (3.5d @1m): live-cadence history must be harvested
continuously or it expires — every un-deployed day loses a day of 1m bars that
B-CAD (lever A/Bs at live-like cadence) will need.

**Changed (1 commit).**
- **`backtest/store.py` (new):** rolling per-(coin,interval) gzipped-JSON store
  under `data/candle_store/` (gitignored). Pure merge core: dedup by open time
  `t`, **fresh wins** (a bar captured mid-formation is finalized by the next
  harvest), ascending, malformed rows dropped. Atomic write-then-rename so a
  crash can't truncate irreplaceable history. `harvest_one` refetches from the
  last stored bar *inclusive*; an empty store reaches back one full retention
  window (5000 bars). Per-pair fetch failures are recorded in the result, not
  raised — one bad coin can't kill the cron sweep.
- **`hlbot harvest-candles`:** sweeps the 10-coin universe × (1m,5m,15m) by
  default; prints an added/total/span/status table; exits non-zero if any pair
  failed (so the systemd run shows red) while still saving the good pairs.
  15m included deliberately: one extra paginated call/coin buys the >52d 15m
  history longer walk-forwards will need.
- **`deploy/systemd/hlbot-harvest.{service,timer}`:** hourly oneshot
  (minute :23, Persistent=true), enabled by install.sh.
- **`deploy/update.sh` fix (found during this work):** auto-update copied new
  unit files into /etc/systemd/system but never *enabled* a brand-new timer —
  the harvest timer would have landed on the live box disabled, silently losing
  the data this task exists to save. Now any repo-shipped `hlbot-*.timer`
  self-enables on a green deploy (idempotent; services stay opt-in).

**Evidence.** **158 tests pass** (7 new in `tests/test_candle_store.py`: merge
semantics, round trip, retention-window reach-back, inclusive incremental
refetch, failure isolation, no-op); ruff clean; `bash -n` clean on both scripts.
**Real-API validation on this box:** 30/30 pairs ok — 5001 bars each, spans
exactly the measured retention (3.5d @1m, 17.4d @5m, 52.1d @15m); second BTC/1m
run added just 1 bar (total 5002, no dups) proving incremental top-up. Store =
3.6MB for full retention; growth ~1MB/day across the grid — negligible.

**What's next (loop).** B-CAD: expose `--vwap-window` in backtest/confirm/
backtest-fetch (window must enter the cache key when ≠60) and A/B regime_filter
+ size_by_signal at 15m/52d and 5m/17d. Filed **B-HIST2** (backtest `--source
store`) so A/Bs can use accumulated history beyond the 5000-bar API window once
it exists.

## Iteration 29 — 2026-06-12 — B-CAD: live-cadence A/Bs — first G0 PASS at the exact live config

**Context.** B-CAD was the top unblocked item: Iter 27 showed the 1h backtest
fades a 60×1h VWAP while live fades a 60×1m VWAP — different strategies — so
every lever verdict to date was rendered at the wrong cadence. Prereq tooling
landed here, then the A/Bs ran on real history (network up; one 429 between
fetches, retried fine).

**Changed (1 commit).**
- **`--vwap-window` exposed in `hlbot backtest` / `confirm` / `backtest-fetch`**,
  threaded through `cached_or_fetch`/`load_frames`.
- **`default_cache_path` keys on the window when ≠60** (`..._w{n}.json.gz`):
  cached frames bake the VWAP window into `candles_1h`/`closes`, so without this
  a window-60 dataset would silently serve a window-4 run. Default window keeps
  the historical key — existing caches stay valid.
- **2 new tests** (160 pass, was 158): key backward-compat + windowed key;
  `cached_or_fetch` never serves the wrong-window cache (fake fetch, tmp DATA_DIR).

**A/B results (twap_mr_v1, 10-coin universe, maker numbers, reproducible via
`--config`/`--vwap-window`).**

*5m / 17d / window=12 (1h VWAP horizon — closest multi-week live proxy):*

| config                     | maker edge | net$  | trades | maxDD  |
|----------------------------|-----------:|------:|-------:|-------:|
| baseline                   |    −1.5bps |  −103 |   3456 | −15.2% |
| regime defaults 0.03/0.65  |    −0.8bps |   −59 |   3476 | −14.9% |
| regime 0.015/0.55          |    −1.6bps |  −103 |   3280 | −17.2% |
| regime 0.01/0.55           |    −1.2bps |   −73 |   3112 | −14.8% |
| regime 0.005/0.55          |    −1.0bps |   −58 |   2780 | −10.5% |
| size_by_signal             |    −0.9bps |   −38 |   3538 |  −9.2% |
| regime + size              |    −0.9bps |   −37 |   3522 |  −9.1% |

Walk-forward (prefer=maker): baseline IS −1.9 / **OOS +1.8bps, +7.9sh** (1158
trades); regime IS −2.1 / OOS +1.7; size IS −2.1 / OOS +1.4. All FAIL G0, but
the most recent ~6d are positive across every config — same pocket live is
printing +29.5bps in.

*15m / 52d / window=4 (coarse z proxy — only 4 closes per window):*
baseline −1.5bps/2162 trades/−9.7%; regime defaults inert (−1.5); size_by_signal
−1.0bps, net −23$ vs −64$, maxDD −4.2%. Confirm: baseline OOS −3.0bps (the
older 52d sample is negative — the positive pocket is recent). FAIL G0.

*1m / 3d / window=60 — the EXACT live strategy (API retains only 3.5d of 1m):*

| exec  | edge    | net$   | trades | win | sharpe | maxDD |
|-------|--------:|-------:|-------:|----:|-------:|------:|
| taker |  −0.0bps|  −0.13 |    846 | 70% |  +0.25 | −3.9% |
| maker | **+5.4bps**| +90.77 | 844 | 72% | +26.70 | −2.0% |

`hlbot confirm` walk-forward: **✅ CONFIRMED — first G0 PASS in this book**
(IS +5.1bps/+24.4sh, OOS +6.0bps/+33.1sh, maker). Honest caveats: 3.5 days
only (walk-forward halves ≈2.3d/1.2d), coincides with the favorable recent
regime, and the engine's maker fills are optimistic (rest-at-mid always fills).
This is *consistency with live*, not yet a durable edge claim.

**Findings.**
1. **Cadence explains the backtest/live divergence**: −5.0bps (1h) → −1.5bps
   (5m/15m proxies) → +5.4bps (exact 1m replica). Lever verdicts rendered at 1h
   were verdicts on a different strategy.
2. **The taker tax is the entire edge at live cadence** (−0.0 taker vs +5.4
   maker). Filed **B-MAKER-LIVE** (human-gated): operator should set
   `HLBOT_TICK_ARGS="--live --execution maker"` — machinery built+tested
   (B2b/B10b). Exits stay taker; worst case is missed entries, not losses.
3. **Lever verdicts at live-like cadence:** regime_filter — helps at 5m
   (−1.5→−0.8, defaults best), inert at 15m, unneeded at 1m where edge is
   already positive; size_by_signal — a loss-dampener, not an edge-adder: cuts
   net loss ~2/3 when edge<0, cut profit 42% at 1m where edge>0. **Both stay
   default OFF; no live changes made.**
4. G0-at-scale needs more 1m history than the API retains → **B-HIST2 promoted
   to top of P0.6** (backtest `--source store`); the hourly harvester (Iter 28)
   is already accumulating the data this needs.

**Evidence.** 160 tests pass; ruff clean. All numbers above from real-API runs
on this box today; datasets cached under `data/backtest_cache/` (incl. new
`_w12`/`_w4` keyed files) for reproducibility.

**What's next (loop).** B-HIST2 once the store has ≥~14d of 1m (check
`data/candle_store/` spans; until then it's API-retention-equivalent), then a
multi-week exact-replica G0. Meanwhile: B-EXIT/B-FUND/B-WIN sweeps can now run
at the proxy cadences with the new flag; B-MAKER-LIVE awaits the operator.

## Iteration 30 — 2026-06-12 — B-HIST2: backtest/confirm `--source store` (tooling ready before the data is)

**Context.** B-HIST2 is the gate to a *durable* edge claim: Iter 29's first G0
PASS at the exact live config (1m, w=60, maker +5.4bps) sits on the only 3.5d
of 1m the API retains. The harvester (Iter 28) is accumulating more, but the
backtester couldn't read it. Built the read path now — pure, offline-testable
code — so the multi-week G0 can run the day the store has the data, and today's
store≈API equivalence gives a free correctness check.

**Changed (1 commit).**
- **`backtest/store.py: frames_from_store`** — builds engine frames from
  `data/candle_store/` instead of the retention-capped API. Candles from the
  store; funding still API-fetched over the candle span (fundingHistory isn't
  retention-limited the same way), seeded 2h before the first bar so the
  carry-forward rate is in effect immediately. `days>0` trims to the most
  recent window; `days=0` = everything stored. Missing store file → loud
  FileNotFoundError naming the pair; a coin trimmed to nothing stays in the
  coverage report (bars=0) instead of vanishing.
- **`StoreCoverage` + `coverage_of`** — per-(coin,interval) bar count, span,
  and interior-gap count. Why: a harvester outage longer than retention loses
  bars forever, and a backtest silently spanning the hole would overstate its
  sample. The CLI prints one line per coin (red if >1% missing or empty).
- **CLI:** `--source api|store` on `hlbot backtest` and `hlbot confirm`;
  `--no-funding` on backtest only (a G0 verdict with funding stripped would be
  dishonest, so confirm always fetches). The duplicated frame-loading branch in
  the two commands is consolidated into `_load_backtest_frames` (net: confirm
  also gains the empty-frames guard it lacked). Store-sourced runs title as
  e.g. `3.5d:store` so screenshots are self-describing.

**Evidence.** **165 tests pass** (5 new: per-bar funding scaling + 2h seed +
exact fetch-args, days-trim window math, missing-pair raise, trimmed-out coin
stays in coverage, interior-gap counting); ruff clean. **Real-data validation
on this box** (store = 10 coins × ~5001 1m bars, 3.5d, 0 missing):
`backtest --source store` → maker **+4.6bps** / 969 trades / +22.7 sharpe,
taker −0.8bps; `confirm --source store --prefer maker` → **✅ G0 PASS**
(IS +4.4bps/+21.3sh, OOS +4.4bps/+21.3sh). Consistent with Iter 29's
API-sourced +5.4bps on a window shifted ~half a day — the store path is
measuring the same thing the API path did.

**What's next (loop).** Filed **B-G014**: the multi-week exact-replica G0
(`confirm --source store --interval 1m --days 0`) once the store's 1m span
≥ ~14d — ETA ~2026-06-26 with the hourly harvester running. Until then the
store adds nothing beyond API retention, so interim iterations should take
B-EXIT/B-FUND/B-WIN sweeps at proxy cadences or the B12 remainder.
B-MAKER-LIVE still awaits the operator (taker −0.8 vs maker +4.6 again
confirms the spread tax IS the edge).

## Iteration 31 — 2026-06-12 — B-EXIT: exit-parameter sweep — one robust lever (stop 0.03), two pruned

**Context.** B-G014 (multi-week exact-replica G0) is blocked until the candle
store accumulates ~14d of 1m bars (ETA ~2026-06-26), so per Iter 30's plan this
iteration took B-EXIT: sweep `sigma_exit` / `stop_loss_pct` / `max_hold_hours`
on twap_mr_v1. All three were already config-plumbed (`--config` overrides,
Iter 22) and all four datasets were already cached, so this is a pure
experiment iteration — **no code change; the increment is evidence**. Engine
clock-freezing verified first (`frozen_clock` patches `time.time()` per frame),
so hold/stop replay is valid at fine cadence.

**Method.** One-dimensional sweeps around the live baseline (sigma_exit 0.5,
stop 0.015, hold 4h) on the exact live config (1m, w=60, 3.5d — the only 1m
the API retains) and the 5m/17d w=12 proxy; direction checks on 15m/52d w=4
and 1h/90d; walk-forward `confirm` on the surviving candidate. 10-coin
universe (ADA…ZEC), maker+taker both reported, all runs from
`data/backtest_cache/` (reproducible offline).

**Results — `stop_loss_pct` 0.015→0.03 is the one robust improvement
(maker edge bps, baseline → wide stop):**

| sample        | baseline | stop 0.03 | net$ change      | maxDD          |
|---------------|---------:|----------:|------------------|----------------|
| 1m 3.5d w60 (live cfg) | +5.4 | **+6.1** | +90.77 → +101.24 | −2.0% → −1.8% |
| 5m 3d w12     |     +5.5 |  **+6.4** | +56.62 → +65.80  | −1.9% → −2.0%  |
| 5m 17d w12    |     −1.5 |  **−0.2** | −102.78 → −15.72 | −15.2% → −12.2%|
| 15m 52d w4    |     −1.5 |  **−1.0** | −63.50 → −42.34  | −9.7% → −8.0%  |
| 1h 90d (NOT live-like) | −5.0 | −5.8 | −140.99 → −156.85 | −21.7% → −23.0%|

Taker at the live config flips −0.0 → **+0.5bps** (matters: live entries are
still taker until B-MAKER-LIVE). Walk-forward at the live config: **G0 PASS,
stronger than baseline** — IS +6.4/OOS +5.7bps maker (vs +5.1/+6.0), full
sample +6.1bps/+28.9sh. 5m/17d confirm still FAILs G0 but improves at every
rung (IS −1.9→−1.5, OOS +1.8→+2.5, all taker-stress rungs better). Only the
1h sample disagrees — and Iter 29 established 1h verdicts are verdicts on a
different strategy. Mechanism: at fine cadence a 1.5% price stop converts
transient wicks into realized losses on trades whose reversion premise was
about to pay; at 1h a 1.5% adverse move is more likely a real trend, so
widening just rides losers. **Not flipped live**: it's a live strategy change
(and per-trade risk loosening — the proposed-overrides channel is
risk-reducing only), and the ≥90d-at-live-cadence bar is unmet (52d max).
Filed as a second confirm arm on B-G014; if it passes and beats baseline on
≥2 weeks of stored 1m, propose the flip to the operator.

**Results — `sigma_exit` is a fair-weather lever (pruned).** Monotone
dose-response on the recent pocket: at 1m, 0.5→0.25→0.1 gives +5.4→+7.0→+8.5
maker (taker −0.0→+1.7→+3.2); same direction on 5m/3d (+5.5→+7.8). But on
5m/17d it's monotone WORSE (−1.5→−1.9→−2.7): the gain is the recent reversion
regime, not cadence — disambiguated by running the same 5m config on both
windows. Boundary check: sigma_exit 0.0 (reversion exit disabled) collapses
the strategy (1m maker +5.4→+2.7, win 70%→47%, trades 844→166) — the
reversion exit IS the profit-taking engine, the stop/hold are just guardrails.
Keep 0.5. (Combo 0.1+stop0.03 hit +10.1bps maker / +4.4 taker on 3.5d —
tempting, but it's the pocket; revisit only if B-G014's multi-week sample
confirms.)

**Results — `max_hold_hours` is inert (pruned).** At 1m: 2/4/8h identical
(no position survives ~2h; reversion or stop fires first), 1h cap +0.1bps
noise. At 5m/17d: 2/4/8 identical, 1h cap worse (−1.7). Keep 4.

**Evidence.** 165 tests pass; ruff clean (no code changed; gate run anyway).
All numbers from cached real-history datasets on this box today; every run
reproducible via the `--config`/`--vwap-window` flags shown.

**What's next (loop).** B-G014 still blocked (~2026-06-26): when it unblocks,
run BOTH arms (baseline + stop 0.03). Until then: B-FUND (funding-aware fade
suppression) and B-WIN (VWAP window study) remain at proxy cadences; B12
remainder (femr_tick preamble consolidation) is the standing refactor item.
B-MAKER-LIVE unchanged, still awaits the operator — note stop 0.03 would make
even taker entries marginally positive at live cadence, but the maker flip is
worth ~10× more (+0.5 vs +6.1bps).

## Iteration 32 — 2026-06-12 — B-FUND: funding_filter lever built, A/B'd, pruned at live cadence (+ funding units fix)

**Context.** B-G014 (multi-week store G0) still blocked (~2026-06-26). Per the
Iter-31 plan, took B-FUND: skip fades that adverse funding would tax while held
(short pays when hourly rate < 0, long when > 0).

**Units fix the lever surfaced (the durable code value of this iteration).**
Live `view.funding` is the HOURLY rate (activeAssetCtx); the backtest engine
was feeding agents `Frame.funding`, the per-bar-scaled accrual rate — 60× too
small at 1m, 12× at 5m. Any rate-threshold lever would have meant a different
thing in backtest vs live, and carry agents backtested at fine intervals would
have read near-zero funding. Fix: `Frame.funding_hourly` (raw rate) populated
by `build_frames`, engine `_view` now passes `funding_hourly or funding`
(fallback keeps legacy 1h frames valid), and `ensure_funding_hourly` backfills
pre-existing fine-interval caches on load (`cached_or_fetch`) by dividing by
bar_hours — so the Iter-31 datasets A/B in correct units. Accrual still uses
the per-bar series; nothing changes for 1h frames (scale factor 1.0).

**Lever.** `TwapMrConfig.funding_filter` (default OFF) +
`funding_max_adverse_hourly` (default 5e-5 = 0.5bp/hr ≈ 4× HL neutral);
pure helper `funding_allows_fade(z, rate, thr)` — unknown rate never blocks.
Applied to entry candidates after the regime filter; baseline path untouched.

**A/B results (maker edge bps, all from the same cached real datasets as
Iter 31; baseline reproduced exactly first: 1m +5.4/+$90.77, taker −0.0).**

| sample                  | base | thr 1.25e-5 | thr 5e-5 | thr 1e-4 |
|-------------------------|-----:|------------:|---------:|---------:|
| 1m 3.5d w60 (live cfg)  | +5.4 |        +4.6 |     +4.4 |     +4.5 |
| 5m 3d w12               | +5.5 |           — |     +5.8 |        — |
| 5m 17d w12              | −1.5 |    **−0.5** |     −0.7 |     −1.5 |
| 15m 52d w4              | −1.5 |           — |     −1.6 |        — |
| 1h 90d (not live-like)  | −5.0 |           — |     −5.1 (inert) | — |

Taker at the live config also degrades (−0.0 → −0.7..−0.9). Walk-forward
confirm of the best arm (5m/17d, thr 1.25e-5): **G0 FAIL** — IS −1.5, OOS
+1.6bps (< +3 bar), though every cost rung beats baseline and maxDD improves
−15.2%→−10.7%.

**Verdict: pruned at live cadence; default stays OFF; no live change.**
The filter removes ~6–13% of entries, and at 1m those skipped fades were
PROFITABLE: extreme funding leaning against a fade marks exactly the crowded
positioning whose unwind the reversion harvests — suppressing them trades away
edge. The 5m/17d gain has a clean dose-response but inverts at the live
cadence on the same calendar window (5m/3d helps +5.5→+5.8 while 1m/3.5d
hurts +5.4→+4.4), so it does not transfer to the live strategy. Negative
result recorded; the lever + correct units stay in the book so B-G014's
multi-week sample can re-test it for one flag if ever warranted.

**Evidence.** 172 tests pass (7 new: funding_allows_fade directionality,
filter skips taxed-fade-only, missing-rate never blocks, builder dual funding
series, engine hourly-view + legacy fallback, ensure_funding_hourly backfill,
cached_or_fetch backfill integration); ruff clean. All A/B numbers from
`data/backtest_cache/` on this box today, reproducible offline via the
`--config` flags shown.

**What's next (loop).** B-G014 still the gate (~2026-06-26, baseline +
stop-0.03 arms). Interim: B-WIN (VWAP window study — cheap, datasets exist for
several windows) or the B12 remainder (femr_tick preamble harness). B-MAKER-LIVE
unchanged, still operator-gated.

## Iteration 33 — 2026-06-12 — B-WIN: VWAP window study — 4h window is the strongest lever yet (+ loop.sh store-continuity fix)

**Context.** B-G014 still blocked (store 1m span 3.5d, need ~14d). Per the
Iter-32 plan, took B-WIN (VWAP window study). All runs from the candle store
(`--source store --days 0`, raw candles → frames at any window, no per-window
API refetch; funding from API; 0 bars missing on all 30 coin×interval pairs).

**Ops finding first (durable fix).** The hourly `hlbot-harvest.timer` is NOT
running on this box (no root / no user systemd bus; store files were 90 min
stale). 1m API retention is ~3.5d, so any >3.5d harvest gap would silently gap
the store and invalidate B-G014's multi-week sample — the top-priority item
was quietly rotting. Fix: `ralph/loop.sh` now tops up the store best-effort at
the start of every iteration (failure logs and continues; `bash -n` clean).
Topped up manually this iteration (30/30 pairs ok, ~90 new 1m bars/coin).

**Sweep — vwap_window at live-like cadences (maker edge bps; 10-coin
ADA…ZEC universe; same stored sample within each column).**

| VWAP span | 1m/3.5d (live cfg) | 5m/17.4d | 15m/52.1d |
|-----------|-------------------:|---------:|----------:|
| 0.5h      | +2.0               | −1.7     | —         |
| 1h (live) | +4.5               | −1.4     | −1.5      |
| 2h        | +4.3               | −0.4     | +0.3      |
| **4h**    | **+7.6**           | **+2.1** | **+1.1**  |
| 8h        | +4.9               | −1.0     | −1.6      |

Monotone rise to an interior peak at 4h on ALL THREE samples — the first
lever that is concordant across cadences AND sample lengths (sigma_exit and
funding_filter both inverted between the 3.5d pocket and 17d). Taker at the
live config flips −0.8 → **+1.7** at w=240 — the only configuration seen so
far whose taker arm is positive. Trades drop 991→328 (deeper 2σ dislocations
of a slower VWAP), so absolute net$ is lower (+$90.01→+$49.66 maker) while
per-trade edge rises 69% and maxDD stays ~−2%; at 5m/17.4d maxDD improves
−15.2%→−5.6% and Sharpe −5.68→+3.45 (first positive multi-week config).

**Walk-forward confirms (store, prefer=maker).**
- 1m w=240: **G0 PASS** — IS +4.4 / OOS **+13.4** / full +7.6bps; taker-1x
  positive (+$11.44). Baseline w=60 same sample: PASS, IS +4.4 / OOS +4.9 /
  full +4.5, taker −0.8. (Both sample-limited: 3.5d.)
- 5m/17.4d w=48: **G0 FAIL but the closest yet** — IS +2.0 / OOS +2.4 vs the
  +3.0 bar (baseline w=12: IS −1.9); both halves positive, every cost rung
  beats baseline.
- **Anti-synergy with B-EXIT's stop:** adding `stop_loss_pct: 0.03` DEGRADES
  the 4h arms on both samples (1m: full +7.6→+5.9, IS +4.4→+1.3 FAIL;
  5m/17d: +2.1→+1.3, IS +0.1 FAIL). B-EXIT's "robust" stop verdict was
  conditional on w=60 — with a 4h σ, 2σ entries are bigger absolute
  dislocations and the wide stop holds losers the slower reversion can't
  rescue. B-G014 arms updated: test stop-0.03 and window-240 SEPARATELY.

**Verdict.** No live change (window is hardcoded 60×1m in the live tick —
cli/main.py ~245–279). Filed **B-WIN2**: parameterize the live VWAP window
(default 60, operator-flippable) so the 4h flip is one config change once
B-G014's multi-week sample rules. Added `--vwap-window 240` as a third B-G014
confirm arm. Volume-weighted σ variant left unexplored (separate slice).

**Evidence.** 172 tests pass; ruff clean (loop.sh is the only code change;
gate run anyway). Every number above reproducible offline-candles via the
exact CLI flags shown (store + `--vwap-window`).

**What's next (loop).** B-WIN2 (small, unblocks the best lever for operator
flip). Then B12 remainder (femr_tick preamble harness — overlaps B-WIN2's
touch point, consider doing together). B-G014 unblocks ~2026-06-26 with three
arms. B-MAKER-LIVE unchanged, operator-gated — note w=240 would make even
taker entries positive at live cadence, but maker remains ~6bps better.

## Iteration 34 — 2026-06-12 — B-WIN2: live VWAP window parameterized (operator-flippable, default 60)

**What.** The live `femr_tick` preamble hardcoded the mean-reversion signal's
VWAP window (60×1m candles) with its own inlined VWAP/σ math — so B-WIN's 4h
window (the strongest lever found: 1m maker +4.5→+7.6bps, concordant across
all three live-like samples) was unreachable without a code edit, and the live
math was a hand-maintained near-copy of the backtester's.

**Change (no live behavior change at defaults).**
- `runtime.resolve_vwap_window(cli, env)` — pure, tested resolver: explicit
  `--vwap-window` > `HLBOT_VWAP_WINDOW` env > 60. Unparseable/sub-floor (<2)
  values fall through to the next source so a typo'd env can never silence the
  signal.
- `femr_tick --vwap-window N` (default 0 = env/60); the dim market line now
  prints `(vwap w=N)` so the operator can verify which window a session ran.
- `_enrich_view(vwap_window=)` fetches `window`×1m candles and computes VWAP/σ
  via the backtester's `rolling_vwap_sigma` + (newly public) `closes_vols` —
  the inlined duplicate is deleted, so live and backtest agree bar-for-bar by
  construction (this was B-WIN2's point: the backtest evidence is only valid
  for live if both run the same math). `view.extra["closes"]` is now the
  window slice, matching backtest `Frame.closes` semantics.
- Operator flip documented in deploy/README.md §Going-live: append
  `--vwap-window 240` to `HLBOT_TICK_ARGS` (or `HLBOT_VWAP_WINDOW=240`) —
  **only after B-G014's multi-week confirm**; still a human-gated live change.

**Behavior notes (intentional, small).** (1) Min-bars guard for a coin to get
a vwap entry was len≥10; it is now `rolling_vwap_sigma`'s floor (window//2,
i.e. 30 at w=60) — stricter, and closer to the backtest's warmup semantics;
only affects coins with <30 1m bars in the last hour (thin/new listings the
volume floor filters anyway). (2) `closes` passed to the regime filter is the
window slice (was: all fetched bars — identical at default since fetch span =
window).

**Evidence.** 176 tests pass (4 new: resolver precedence + garbage fallback;
fake-httpx `_enrich_view` test proving fetch span follows the window and
vwap/σ/closes equal `rolling_vwap_sigma` output; too-few-bars skip). Ruff
clean. No edge claim — this is plumbing; the numbers stay B-WIN's (Iter 33).

**What's next (loop).** B12 remainder (fold the rest of the femr_tick preamble
— clearinghouse fetch → risk-cap → view enrich — into a shared harness) or
B-EDGE2 (second edge hunt). B-G014 unblocks ~2026-06-26 (store 1m span ≥14d;
loop.sh tops the store up every iteration) with three arms: baseline,
stop 0.03, w=240 — w=240 is now one config flip away if it rules.

## Iteration 35 — 2026-06-12 — B-EDGE2: breakout_v1 (Donchian momentum) — G0 PASS on 52d, the first taker-positive multi-week strategy

**What.** The book is single-strategy (twap_mr mean reversion); carry is pruned.
Built the canonical low-correlation complement: `agents/breakout.py`
(`breakout_v1`) — close-channel (Donchian) time-series momentum. Long when mid
breaks the prior `lookback_bars` close-high, short on the low; exit on the
opposite `exit_lookback_bars` channel, ±3% stop, or 24h max hold; 1h re-entry
cooldown; same liquidity floor / notional caps / audit-log replay pattern as
twap_mr. Registered in `_backtest_factories` ("breakout_v1"); config in BARS so
one agent sweeps any cadence (`--vwap-window ≥ lookback+1` carries the closes).

**Sweep (store, 10-coin ADA…ZEC, edge bps, funding from API).**

| Channel | 15m/52.2d taker | 15m/52.2d maker | trades |
|---------|----------------:|----------------:|-------:|
| 4h  (lb=16)   | −7.4  | −1.9  | 3250 |
| 24h (lb=96)   | +3.0  | +8.4  | 872  |
| 48h (lb=192)  | +12.6 | +18.1 | 502  |
| **96h (lb=384)** | **+36.4** | **+41.9** | 322 |

Monotone dose-response in channel length; 96h: win 53–55%, Sharpe +4.2/+4.9,
maxDD −9.5/−9.1%. Other cadences, shorter channels: 5m/17.4d 24h-channel taker
−2.1 / maker +3.6; 1m/3.6d 4h-channel taker −13.9 / maker −8.4 — the edge
lives at the 48–96h horizon on 15m bars, NOT at the live 1m cadence (and the
3.6d pocket where twap_mr earns +7.6 is exactly where breakout bleeds —
the anti-correlation thesis showing up in-sample).

**Walk-forward confirms (G0 as code, prefer=maker, store).**
- lb=384/ex=96: **PASS** — IS +26.0bps/sh +4.15 (226 tr), OOS +75.9/sh +6.02
  (96 tr); cost ladder positive through taker-3× (+32.7bps); 2× slippage ok.
- lb=192/ex=48: **PASS** — IS +7.8, OOS +39.3, taker-3× +8.8 (adjacent config
  passes → not a knife-edge parameter).
- ex-ZEC lb=384: **PASS** — full maker +24.0/taker +18.5 (302 tr), OOS +68.5;
  honest flag: IS sharpe 0.98 vs the 1.0 bar (rounding-marginal). ZEC
  contributed (~46% of net$) but the edge is not a one-coin artifact.

**Caveats (why this is NOT live-ready).** (1) One 52.2d regime sample —
momentum is regime-fragile; revalidate as the store grows (B-EDGE2b). (2) The
engine's maker fill (rest at mid, always fills) is optimistic for momentum —
posting into a running breakout often misses; treat the TAKER arm (+36.4 /
+18.5 ex-ZEC) as the honest number. Taker is also what live does today.
(3) Live plumbing doesn't exist: `_enrich_view` fetches 1m candles, capped
~5000 by the API — a 96h channel needs 385×15m bars (B-EDGE2a). (4) Bar-close
fills, no intra-bar stop modeling — same limits as every backtest here.

**Evidence.** 191 tests pass (15 new in tests/test_breakout.py: channel math
incl. current-bar exclusion + buffer, entry/exit/cooldown/volume-floor/ranking
decide() coverage, trend-profits + chop-stays-flat engine runs, factory
registration); ruff clean. All numbers reproducible via the CLI flags above
(store candles committed to disk locally, funding from API).

**What's next (loop).** B-EDGE2a (paper wiring at 15m cadence — design the
candle source first: store-read vs 15m API fetch), B-EDGE2c (PnL correlation
vs twap_mr), B12 remainder. B-G014 unblocks ~2026-06-26. No live change here:
breakout_v1 exists only behind the backtest CLI.

## Iteration 36 — 2026-06-12 — B-EDGE2c: breakout_v1 ⊥ twap_mr_v1 — daily-PnL corr ≈ −0.1, diversification thesis substantiated

**What.** Iter 35 claimed breakout_v1 diversifies the twap_mr book because it's
"a different signal family" — thesis, not evidence. Built the measurement:
`backtest/correlate.py` (pure: UTC-day PnL bucketing from equity curves +
Pearson; flat days kept — "A trades while B sits out" IS the diversification;
days aligned by intersection so a longer warmup can't skew the series) and
`hlbot correlate` (two arms, per-arm `--config`/`--vwap-window` since breakout
needs window ≥ lookback+1 while twap wants its own cadence proxy; same frames
source + cost model for both arms).

**Numbers (store, 15m × 52.2d × 10-coin ADA…ZEC, 0 bars missing, funding from
API, breakout = lb 384 / ex 96, 54 overlapping UTC days).**

| twap arm | mode | twap edge | breakout edge | daily-PnL corr |
|----------|------|----------:|--------------:|---------------:|
| w=4 (live-config cadence proxy) | taker | −6.8bps | +36.4bps | **−0.08** |
| w=4 | maker | −1.4 | +41.9 | **−0.16** |
| w=16 (B-WIN 4h-window candidate) | taker | −4.4 | +36.4 | **−0.07** |
| w=16 | maker | +1.1 | +41.9 | **−0.10** |

Consistently ~zero-to-slightly-negative across both twap configs and both cost
models. Breakout +36.4/+41.9 (322 tr) and twap w=16 maker +1.1 exactly
reproduce Iters 35/33 — the harness replays deterministically. The in-sample
anecdote from Iter 35 (breakout bleeds in the 3.6d pocket where twap earns)
generalizes: the two PnL streams move independently day-to-day.

**Honest caveats.** (1) n=54 days → CI95 ≈ ±0.27; the defensible claim is
"uncorrelated", NOT "anti-correlated hedge". (2) The twap arm is the 15m
cadence *proxy* of the live 1m config — the literal live strategy gives only
~3.6d ≈ 4 daily points, too few to correlate; revisit at 1m when the store
matures (B-G014 sample). (3) One 52d regime sample, same caveat as B-EDGE2
itself — corr is regime-dependent; rerun with B-EDGE2b revalidations.
(4) Curve totals vs scorecard net differ by the end-liquidation fee (booked
after the last equity point) — visible in the CLI output, ~$0.1 on $1k.

**Evidence.** 201 tests pass (10 new in tests/test_correlate.py: day
bucketing incl. baseline/out-of-order/gap-day/empty, Pearson exact ±1 +
undefined cases, intersection alignment, anticorrelated-arms end-to-end);
ruff clean. All numbers reproducible via the CLI flags above.

**What's next (loop).** B-EDGE2a (paper wiring for a 15m-cadence agent — the
correlation number justifies the plumbing investment), B12 remainder.
B-G014 unblocks ~2026-06-26 (three confirm arms). On B-EDGE2b reruns, also
rerun `hlbot correlate` — a corr that drifts toward +1 in a new regime would
gut the second-edge case even if breakout's own edge holds.

## Iteration 37 — 2026-06-12 — B-PAPER: the paper book now exists, and paper/live books never mix

**What.** Scoping B-EDGE2a ("roster entry paper-only") exposed that there is no
such thing as a paper book: `femr_tick` without `--live` never logged
place/flatten at all — `defer_exec_logging=True` defers logging to the live
execution loop, which paper mode returns before reaching. Verified against
pre-extraction history (the gap dates to the original `femr_tick`, not the
Iter-14 refactor): every "paper default" pilot to date (twap_mr_regime_v1 etc.)
accumulated zero evidence, and paper agents could never see their own positions
(no exits, no cooldowns, would re-enter forever). Fixed as the foundation slice
of B-EDGE2a:

- `femr_tick` paper ticks log place/flatten at gather time as `is_paper=1`
  (`defer_exec_logging=live`); live behavior unchanged (log-after-confirm).
- Book separation everywhere the decision log is replayed, since mixed-book DBs
  are now possible: agents replay the book matching the tick mode via
  `Agent.paper_book` (set by `gather_decisions` before `decide()`; default True
  == the backtest engine's `is_paper=1` logging, so backtests are untouched) —
  all 7 position-replaying agents filtered. `bot_owned_coins` and
  `coin_in_cooldown` default to the LIVE book (`paper=False`), so a paper row
  can never reclassify a manual position as bot-owned (the don't-touch
  protection), never gates a live entry, and a live tick can never flatten a
  phantom paper position. `reconcile_positions` is pinned live-book-only and
  `femr_tick` now runs reconcile only in live mode — exchange truth says
  nothing about paper positions; before this, the first paper position would
  have been force-flattened as "stale" on the next tick.
- femr's entry scan counts its own replayed positions as active (union with
  exchange positions) — in live this is a no-op (reconcile clears strays
  pre-decide); in paper it stops infinite re-entry of the same coin.

**Why it matters.** Promotion needs forward-test evidence (G1); breakout_v1 and
every future candidate get their paper track record from exactly this machinery.
This also closes a real live-safety hole *before* it could open: had paper
logging been "fixed" naively (or had anyone run a paper tick against the live
DB after such a fix), unfiltered replays would have let paper rows trigger real
flattens and strip manual-position protection.

**Evidence.** 215 tests pass (12 new: tests/test_paper_book.py — replay-book
filter parametrized over all 7 agents, owned/cooldown book params, reconcile
never touches the paper book, ownership classification per book, femr
no-re-entry + control; test_gather_decisions.py — paper_book set to match tick
mode, paper femr policy logs exec rows as is_paper=1; one existing cooldown
test updated to seed a live-book row, matching what the live path writes).
Ruff clean. Live-fire on a scratch DB (real API, 3 paper ticks): tick 1 logged
3 paper places (femr XMR, twap_mr TRX, twap_mr_regime TRX) all `is_paper=1`;
tick 2 showed `bot-owned: ['TRX','XMR']` with twap_mr holding instead of
re-entering; tick 3 (post femr fix) femr holds at capacity, no duplicate place.
Docs: deploy/README §"Paper book" (run a parallel paper loop with
`HLBOT_DB=data/hlbot_paper.sqlite`). No edge claim — this is measurement
infrastructure; no live behavior change (live rows were always `is_paper=0`,
filters preserve them exactly).

**Known limitation (filed B-PAPER2).** femr's EXIT engine evaluates exchange
positions only, so a paper femr position never exits — it now just holds a
capacity slot instead of spamming re-entries. Fix is to synthesize paper
`live_positions` from the paper-book replay; other agents exit fine (their
exits replay their own log).

**What's next (loop).** B-EDGE2a remainder, now meaningful: 15m closes feed in
`_enrich_view` (`closes_15m`, one ~385-row API call per top coin, gated on
breakout in roster), breakout `closes_key` config, roster entry with the
validated lb=384/ex=96 config + goals yaml. Then the paper book starts
accumulating breakout's G1 evidence while B-G014 waits on the store
(~2026-06-26).

## Iteration 38 — 2026-06-12 — B-EDGE2a: breakout_v1 is in the paper roster — the second edge starts its forward test

**What.** Wired the G0-validated breakout config (Iter 35: 96h Donchian
channel on 15m bars, taker +36.4bps / 52d) into the live tick as a paper-only
roster agent, completing B-EDGE2a on the Iter-37 paper-book foundation:

- `BreakoutConfig.closes_key` (default `"closes"`) — the agent reads its
  trailing closes from a configurable `view.extra` key, so backtests keep
  consuming frame closes unchanged while the roster entry consumes a
  dedicated 15m feed.
- `_enrich_view(closes_15m_bars=N)` — fetches N×15m candles per top-20 coin
  into `view.extra["closes_15m"]` (in-progress bar last, matching backtest
  frame semantics; per-coin error isolation like the 1m loop). Sized by the
  new `runtime.closes_15m_bars(agents)`: max(lookback, exit_lookback)+1
  across roster agents that declare `closes_key == "closes_15m"`, 0 when none
  — and in live mode `_filter_live_agents_by_state` drops unpromoted agents
  BEFORE the feed size is computed, so live ticks pay zero extra API calls
  until an operator promotes breakout (the no-silent-cost gate the backlog
  asked for).
- Roster entry: lb=384/ex=96 (the validated 96h/24h channels), $20/trade,
  $60 book — femr-scale paper sizing. `configs/breakout_v1.yaml`: paper mode,
  guardrails sized to the $60 book (pause at −$15/24h, demote at 7d edge
  <−20bps), promotion paper→live_small only, gates at ~half the backtest edge
  (30d: edge ≥15bps, net ≥$5, ≥60 trades).

**Why.** Diversification before AUM: breakout is the only candidate that
passed G0 with positive taker edge (Iter 35) and is ~uncorrelated with
twap_mr (Iter 36, corr ≈ −0.1). G1 needs forward-test evidence, which only
starts accumulating once the agent actually runs — every week of delay is a
week of missing track record while B-G014 waits on the store (~2026-06-26).

**Evidence.** 220 tests pass (5 new: closes_key routes entry+exit feeds in
test_breakout.py; closes_15m_bars roster scan incl. exit-channel-longer case
and the bars=0/no-traffic path in test_tick_harness.py with an
interval-aware fake client; breakout_v1.yaml loads paper-mode with
live_small-only promotion in test_supervisor_configs.py); ruff clean.
Live-fire (real API, scratch DB, 2 paper ticks): tick 1 — `closes15m: 20
coins (≤385 bars)`, breakout_v1 entered XPL long (+1.05% beyond the 384-bar
channel) logged `is_paper=1`, while twap_mr SHORTED the same coin — the
opposite-regime construction visible on the very first tick; tick 2 —
bot-owned [XMR, XPL], breakout held (no re-entry), hold line correctly
reports "w/ closes_15m". Docs: deploy/README §Paper book notes the roster
addition + its API cost. No edge claim beyond Iter 35's; no live change
(paper rows only; live roster/feed gated as above).

**Honest caveats.** (1) Cadence fidelity: the backtest decides on 15m bar
CLOSES; the live loop evaluates the current mid every tick, so an intra-bar
wick that retraces by the close can enter live-paper where the backtest
wouldn't — expect the paper track to be slightly more trigger-happy than G0;
judge G1 accordingly. (2) Discovered and filed B-PAPER3: `score_agent` is
fills-based, so the supervisor sees N/A for every paper-only metric — the
paper book RECORDS evidence but nothing SCORES it yet; breakout's declared
promotion gates are inert until a paper-PnL scorecard exists (and promotion
stays human-gated regardless). (3) Top-20-by-volume universe ≠ the 10-coin
backtest universe; coins drift in/out with volume rank.

**What's next (loop).** B-PAPER3 (paper scorecard — turns the accumulating
book into readable G1 numbers), B-PAPER2 (femr paper exits), B-EDGE2b
re-confirm as the store grows, B12 remainder. B-G014 unblocks ~2026-06-26.

## Iteration 39 — 2026-06-12 — B-PAPER3: the paper book is now readable — `hlbot score --paper`

**What.** The forward-test evidence the paper book records (B-PAPER/B-EDGE2a)
was write-only: `score_agent` is fills-based and a paper book produces no
fills, so every metric for a paper-only agent (breakout_v1) was N/A. New
`scoring/paper.py` closes the loop:

- `replay_paper_fills` — pure replay of one agent's is_paper=1 place/flatten
  rows into synthetic fills, using the SAME book semantics as the agents' own
  `_position_state` replays (place opens / overwrites, flatten closes the full
  held size; unfillable rows skipped). Execution is modeled by the
  backtester's `CostModel` (default taker: 4.5bps fee + 2bps slippage per leg,
  slippage folded into the effective fill px exactly like the engine), so
  paper numbers are directly comparable to G0 backtest numbers.
- `score_paper_agent` — aggregates those fills into the SAME `Scorecard`
  shape/semantics as `score_agent` (each leg counts in its window like a
  fill, win stats on closes, daily-PnL Sharpe, capital-based drawdown/Calmar),
  so everything that reads scorecards can read paper ones.
- `hlbot score --paper` — prints the paper cards + an "open paper positions"
  table (entry px, age; explicitly NOT marked to market).
- Operator docs: deploy/README §Paper book — how to read the paper loop's
  book, with the caveats spelled out.

**Why.** breakout_v1's G1 evidence is accumulating on the deploy box but its
declared promotion gates (30d edge ≥15bps, net ≥$5, ≥60 trades) could never be
checked against anything — manual log reads don't scale and invite wishful
interpretation. Now the gate metrics come from the same Scorecard machinery
the supervisor already uses. Promotion stays human-gated; this is the
evidence READOUT, not an auto-promoter.

**Evidence.** 233 tests pass (13 new in tests/test_paper_score.py: round trips
long/short at zero cost, taker fee+slippage arithmetic matches engine
semantics hand-computed, flatten-without-open and unfillable rows skipped,
re-place overwrites like the agent book, window filtering at fill granularity
(entry outside / exit inside 24h counts the exit only), live rows invisible to
the paper score, Sharpe needs ≥3 days / DD needs capital_base, roster +
score_paper_all coverage, default-cost flat round trip loses ~6.5bps). Ruff
clean. Smoke: scratch DB with a seeded XPL round trip + 2 open positions →
table shows +$1.16 net (hand-checked: +$1.19 price − $0.03 modeled fees);
open-position edge −4.5bps = exactly the one-leg taker fee. Empty real DB →
graceful empty table. No backtest pollution risk: all backtest/confirm paths
use `init_db(":memory:")`, so a production DB's paper rows come only from real
paper ticks.

**Honest limits (filed as backlog items).** (1) funding_pnl=0 — femr's paper
book can't be judged until funding accrual is modeled (B-PAPER3a; moot until
B-PAPER2 gives femr paper exits anyway). (2) Realized-only — open positions
contribute entry fees but no mark-to-market, matching live scorecard
semantics. (3) Cost model is assumed, not measured: paper "fills" never faced
queue/latency reality, so treat paper edge as backtest-grade, not live-grade,
evidence. (4) Not yet in track-record/goal evaluation (B-PAPER3b).

**What's next (loop).** B-PAPER2 (femr paper exits — femr paper positions
currently hold slots forever and show as stale opens in the new table),
B-PAPER3b (track-record paper section), B12 remainder. B-G014 unblocks
~2026-06-26 (store 1m span ≥14d).

## Iteration 40 — 2026-06-12 — B-PAPER2: femr paper positions can finally exit

**What.** femr's exit ladder (section 1: stop-loss / take-profit / max-hold /
funding-normalized) only evaluates coins present in
`view.extra["live_positions"]` — exchange truth, "adopt" semantics. A paper
position has no exchange counterpart, so a paper femr position could NEVER
exit: it held one of femr's 2 capacity slots forever and showed up as a
permanently-stale open in the B-PAPER3 table. Fix, exactly as scoped in the
backlog:

- `runtime.synthesize_paper_positions(conn, agent, mids)` — pure replay of the
  agent's paper book (is_paper=1 place/flatten, same book semantics as the
  agents' own replays and `replay_paper_fills`: place opens / re-place
  overwrites / flatten always closes; unfillable rows skipped — a 0 entry px
  would divide-by-zero in femr's return math) into the clearinghouse position
  dict shape, mirroring the backtest engine's own `_view` synthesis: szi/entry
  from the log, position_value + unrealized_pnl marked at the current mid
  (entry fallback when no mid), liquidation_px=0.0 (femr skips liq-proximity
  at ≤0), no funding accrual.
- `femr_tick` paper mode passes that synthesized list as
  `view.extra["live_positions"]`; live mode is byte-for-byte the old behavior
  (exchange positions ∩ live-book ownership). One exit path now runs in all
  three modes (backtest / paper / live).

**Why.** The paper book is the forward-test evidence pipeline (B-PAPER →
B-EDGE2a → B-PAPER3). Without exits, femr's paper track could never produce a
closed round trip — no realized PnL, no edge number, capacity slots leaking
away — and the B-PAPER3 scorecard for femr was structurally meaningless.

**Evidence.** 239 tests pass (6 new in test_paper_book.py: long/short dict
shapes incl. uPnL sign + mark-at-mid, flatten-closes + re-place-overwrites,
unfillable/live rows invisible, mid-missing falls back to entry,
parametrized end-to-end femr STOP-LOSS and TAKE-PROFIT exits whose logged
paper flatten closes the book for the next tick). Ruff clean. Live-fire
(real API, scratch DB): seeded a paper BTC long at mid×0.98 → tick 1 logged
`FEMR-EXIT BTC: TAKE-PROFIT (+2.04%)` is_paper=1 at the real mid ($63,380.5)
and rotated the freed slot into a fresh XMR funding short; tick 2 held (no
phantom re-flatten, no duplicate); `hlbot score --paper` now shows the femr
round trip realized (+$0.23 net, +50.5bps on the seeded move) instead of a
stale open. No live change: live ticks still read exchange truth + live book
only.

**Honest caveats.** (1) Paper exits price at the tick-time mid with modeled
costs — no queue/latency reality; paper edge stays backtest-grade evidence.
(2) funding_pnl in paper scorecards is still 0 (B-PAPER3a, now genuinely the
last gap for judging femr's paper book — holds exist AND close as of this
iteration). (3) The exit fires at tick cadence, not continuously: a wick that
breaches the stop intra-tick and retraces is invisible, same as live.

**What's next (loop).** B-PAPER3b (paper section in track-record / goal
readout), B-PAPER3a (modeled funding accrual over paper holds), B12 remainder
(fold the femr_tick preamble into a shared harness), B-EDGE2b re-confirm as
the store grows. B-G014 unblocks ~2026-06-26 (store 1m span ≥14d).

## Iteration 41 — 2026-06-12 — B-PAPER3a: paper scorecards model funding accrual

**What.** The last structural gap in judging femr's paper book: paper
scorecards hard-coded `funding_pnl=0`, so a funding strategy's entire revenue
line was invisible (price-only femr paper cards could only ever show fee
bleed). Now `scoring/paper.py` models accrual from funding-rate history:

- `PaperHold` + `replay_paper_holds` — replays the is_paper=1 book into hold
  spans under the SAME rules as `replay_paper_fills` (re-place overwrite
  DROPS the old hold — its exit never fills, so it accrues nothing either;
  flatten always ends the hold even with a missing px; unfillable places open
  nothing; open holds run to now).
- `modeled_funding_events(holds, funding_by_coin, now_ms)` — each HL
  `fundingHistory` row inside a hold (strictly-after entry, at-or-before
  exit/now) is one cash event: `usdc = -signed_sz × entry_px × rate`, the
  backtest engine's accrual marked at the entry mid (offline proxy for the
  hourly mark; fine at femr's hours-scale holds, drifts on multi-day trends).
- `score_paper_agent(..., funding_by_coin=)` — events are window-filtered by
  their OWN timestamp and bucketed into daily PnL on their own day, exactly
  mirroring how `score_agent` treats live `funding_payments` (funding is cash
  when paid, not a close-time realization — an open femr hold's carry is
  visible without waiting for the exit). Win stats stay price-based, also
  like live.
- `paper_funding_spans(conn)` — per-coin (min entry, max exit/now) span the
  CLI must fetch. `hlbot score --paper` now fetches rates per coin
  (`--no-funding` to skip; per-coin fetch failure warns and degrades that
  coin to 0, never crashes the readout) and the score table grew a `funding`
  column (live cards show attributed funding there too).

**Why.** B-PAPER2 (Iter 40) gave femr paper round trips, but a femr card
without funding is structurally meaningless — the strategy exists to collect
carry. This was filed as the "genuinely the last gap" for femr paper
judgeability. Accrual semantics deliberately mirror live attribution so paper
cards and live cards mean the same thing column-for-column.

**Evidence.** 251 tests pass (12 new in tests/test_paper_funding.py: hold
replay pins overwrite-drop/orphan-flatten/missing-px-flatten semantics
against the fills replay; long-pays/short-collects arithmetic exact
(−signed×100×1e-4 = ∓0.02); entry-exclusive/exit-inclusive boundaries; open
hold accrues to now; zero-rate/unknown-coin emit nothing; scorecard
integration: funding in net/edge, 24h window keeps only the recent event of a
3d hold, funding-only daily series feeds Sharpe (carry agent scores without a
close), spans cover multi-agent books + open-to-now, live rows ignored;
funding_by_coin=None keeps offline behavior). Ruff clean. Live-fire (real
API, scratch DB): seeded 10h BTC paper short closed 2h ago → 10 hourly events
fetched, funding +$0.08 on $1k notional folded into net (−1.22 = −0.40
slip-cross −0.90 fees +0.08, hand-checked exactly); open 6h ETH long accrues
−$0.01 (longs pay) up to now; `--no-funding` prints the old funding=0 title
with zero network calls. deploy/README §Paper book updated.

**Honest caveats.** (1) Accrual marks at the ENTRY mid, not an hourly mark —
on a multi-day breakout hold with a big trend move the modeled funding
notional drifts from truth (the rate×direction is exact, the notional is
stale). Hourly marks would need candle history per hold (the store only
covers the 10-coin universe; paper coins roam top-20+) — revisit only if
breakout funding ever becomes decision-relevant. (2) Modeled, not paid: no
queue/latency/socialized-funding reality; paper stays backtest-grade
evidence. (3) `hlbot score --paper` now makes one paginated API call per
paper coin by default — fine at today's book size (a handful of coins).

**What's next (loop).** B-PAPER3b (surface paper cards in track-record +
goal readout — femr's paper card is now fully judgeable so the wiring has
real payload), B12 remainder (femr_tick preamble harness), B-EDGE2b
re-confirm as the store grows. B-G014 unblocks ~2026-06-26 (store 1m span
≥14d).

## Iteration 42 — 2026-06-12 — B-PAPER3b: track record grows a paper section

**What.** The track record (the Path-C artifact capital decisions are made on)
now surfaces the paper book instead of hiding it behind a separate CLI:

- `build_track_record` adds `paper_agents` (+ a `paper_note` disclaimer):
  per-agent paper cards from `score_paper_agent` ("all" + 24h/7d/30d windows,
  modeled taker costs + modeled funding when rates are supplied) plus
  `open_positions`. New `scoring.paper.paper_daily_pnl` returns the
  gap-filled daily net series (fills' closed_pnl−fee on their day, modeled
  funding events on their own day, zero-filled between first and last active
  day) so the report's sharpe(d)/maxDD$ columns are computed by the SAME
  `_daily_sharpe`/`_dollar_max_drawdown` helpers as the live table —
  column-for-column comparable.
- md/html render it as "Paper agents (NOT live)" with the forward-test
  disclaimer; JSON carries `paper_note`. The live per-agent table and the
  account equity curve stay fills-based.
- Found while wiring: `list_agents` unions `agent_decisions`, so paper-only
  agents have ALWAYS appeared in the live table as zero-trade clutter rows.
  Now an agent with no fills whose only presence is the paper roster is
  excluded from the live table (its record lives in the paper section); an
  agent with both books shows in both (pinned by test).
- `hlbot track-record` fetches funding-rate history for the paper section by
  default via `_fetch_paper_funding` (extracted from `score --paper`, now
  shared; per-coin failure degrades that coin to funding=0 with a warning;
  zero network calls when there is no paper book or with `--no-paper-funding`).

**Why.** B-PAPER3b: the paper book is the forward-test evidence pipeline for
breakout_v1/femr, but the evidence only existed in an ad-hoc CLI readout. The
track record is the artifact the operator (and eventually allocators) reads —
candidates' forward tests belong there, clearly separated from exchange truth
so the headline record can never flatter.

**Evidence.** 256 tests pass (5 new: paper section numbers exact vs modeled
costs (net +9.7735 on a 100→110 round trip + open entry), paper-only agent
excluded from live table, both-books agent in both tables with per-book
trade counts, funding threading (−$0.01 on a 1×100×1e-4 long hold) folded
into net, no-paper-book → no section/key, `paper_daily_pnl` gap-fill +
funding-on-own-day + empty-book). Ruff clean. Live-fire (real API, scratch
DB): `hlbot track-record` rendered twap_mr_v1 (3 fills) in the live table
ONLY, breakout_v1 closed round trip with real modeled funding −$0.08 over a
10h BTC long (longs pay) and femr_v1's open ETH short accruing +$0.01, both
in the paper section ONLY; `--no-paper-funding` made zero httpx calls; JSON
has paper_note/windows, HTML has the labeled section. deploy/README §Paper
book documents the new section.

**Honest caveats.** (1) Paper cards inherit all B-PAPER3/3a limits (modeled
fills, entry-mid funding marks, realized-only price PnL) — the disclaimer
line travels with the table. (2) `hlbot track-record` now makes one
funding-history call per paper coin by default (was fully offline); no-op
without a paper book, `--no-paper-funding` restores offline.

**What's next (loop).** B-PAPER3c (goal evaluation on paper cards —
pause/demote only; promotion MUST be suppressed there since `run_once`
auto-applies promote actions — design note in the backlog), B12 remainder
(femr_tick preamble harness), B-EDGE2b re-confirm as the store grows.
B-G014 unblocks ~2026-06-26 (store 1m span ≥14d).

## Iteration 43 — 2026-06-12 — B-PAPER3c: the supervisor reads the paper book (pause/demote only)

**What.** `supervisor.goals.evaluate` now sources an agent's scorecards from
its paper book (`score_paper_agent`, modeled costs + optional modeled funding)
when the agent's *effective* mode is paper and it has paper rows; everything
else stays fills-based. Specifics:

- **Effective mode, not YAML mode:** a new `_effective_mode` reads the
  `agent_state` row (where supervisor actions and operator promotions land),
  falling back to the YAML's declared initial `mode:`. The promotion gate now
  keys on the effective mode too — fixing a latent wart where an agent already
  promoted in the DB could be "re-promoted" forever off the stale YAML value.
- **Guardrails fire on paper evidence:** pause/demote/alert evaluate against
  the paper card. Every paper-sourced row in `goal_evaluations` carries a
  `[paper]` detail prefix so the audit trail can never pass paper evidence
  off as exchange truth (Evaluation grew a `source: fills|paper` field).
- **Promotion can NEVER apply from paper:** when every promotion condition
  passes on a paper card, the evaluation is downgraded to status=pass /
  action=none with detail "promotion-ready paper -> live_small on paper
  evidence — human-gated, not applied". Defense in depth: `run_once` also
  refuses to `_set_mode` any promote whose source is paper (logs an error)
  even if one were ever emitted.
- **Funding threading:** `supervise`/`run_once`/`evaluate` accept
  `paper_funding_by_coin`; `hlbot supervisor` fetches it by default via the
  shared `_fetch_paper_funding` (`--no-paper-funding` opt-out) so femr's
  paper card isn't judged on funding=0 — the exact spurious-pause trap
  B-PAPER3a warned about.

**Why.** The paper book is the forward-test pipeline for breakout_v1/femr,
but the supervisor was blind to it: fills-based cards for paper-only agents
are permanently N/A, so the pre-declared breakout guardrails ($15/24h pause,
-20bps/7d demote) could never fire and a bleeding paper candidate would have
accumulated "evidence" unguarded forever. Now the same YAML contract polices
both books, while the live-gate hard rule (paper evidence never flips a mode)
is enforced at two layers.

**Evidence.** 263 tests pass (7 new in tests/test_paper_goals.py: real
breakout_v1.yaml pause guardrail fires on a -$20 paper round trip +
agent_state disabled + all audit rows `[paper]`-tagged; promotion-ready paper
book yields action=none/"human-gated" and run_once applies nothing; effective
mode picks fills over a stale bleeding paper book (live_small agent not
paused, metric=+1.9 not -250); no re-promotion off stale YAML mode; funding
threading shifts the guardrail metric by exactly the modeled +$10 event;
monkeypatched rogue paper-promote is refused by run_once; no-paper-book agent
stays fills-sourced with unprefixed details). Ruff clean. Live-fire (real
API, scratch DBs): bleeding XPL paper book → `hlbot supervisor` fetched real
XPL funding history, fired PAUSE + DEMOTE (enabled=0, paused_reason set, all
6 audit rows `[paper]`-prefixed); 31-round-trip winning book clearing all
breakout promotion gates → "no actions taken", promotion row reads
"[paper] promotion-ready paper -> live_small … human-gated, not applied",
zero agent_state rows written, zero network calls with `--no-paper-funding`.

**Honest caveats.** (1) Paper guardrail decisions inherit every B-PAPER3/3a
modeling limit (synthetic taker fills, entry-mid funding marks,
realized-only) — a paused paper agent is a modeling verdict, not exchange
truth; the `[paper]` tag keeps that visible. (2) `hlbot supervisor` now makes
one funding-history call per paper coin per run (every 5 min via
run-tick.sh); spans grow with the paper book's age since the "all"-window
card needs full-history accrual — fine at today's handful of coins, revisit
(cache or recent-window-only fetch) if the paper roster widens a lot.
(3) A paused paper agent stops accumulating forward-test evidence until an
operator re-enables it — that's the intended tripwire, but it means paper
G1 samples can have guardrail-shaped holes; the pause is visible in
`agent_state.paused_reason` and the audit trail.

**What's next (loop).** B12 remainder (femr_tick preamble harness), B-EDGE2b
re-confirm as the store grows (15m span check), B1c remaining hypotheses
(beta-neutral xfund cross-section / lower cadence) if idle. B-G014 unblocks
~2026-06-26 (store 1m span ≥14d — check `hlbot harvest-candles` spans).

## Iteration 44 — 2026-06-12 — B12(g): account fetch + risk-sizing values extracted to the tick harness

**What.** The last inlined, untested network block in the `femr_tick` preamble
— the httpx clearinghouse/spot fetch and the derived sizing values — is now
`runtime.fetch_account_state(base_url, address) -> AccountState`
(clearinghouse + spot payloads ride along raw; `account_value`, `spot_usdc`,
`portfolio_value`, `withdrawable` derived once via the tested
`risk.scaling` parsers). `femr_tick` calls it and keeps only the
`compute_notional_cap` call + the console summary; `positions_from_clearinghouse`
now reads `account.clearinghouse`. Failure semantics preserved exactly and
now *documented in code*: a perp `clearinghouseState` HTTP failure propagates
(a tick must never size risk blind), a spot failure degrades to `{}` →
$0 spot USDC, which only shrinks portfolio value and hence the notional caps
(tightening, never loosening). Two incidental hardenings: `account_value`
now uses `perp_account_value_from_state` (malformed strings → 0 instead of
ValueError) and a malformed `withdrawable` degrades to 0.0 instead of
crashing the tick.

**Why.** B12/REVIEW M3: live-path code that isn't importable isn't testable.
This was the slice that decides how much the bot is *allowed to risk* — the
5×/1× caps key off `portfolio_value` — and its error behavior (what happens
when HL's spot endpoint hiccups mid-tick?) had zero test coverage. Series
now: (a) execute_decisions … (g) fetch_account_state; remaining is overrides
loading, roster construction, and `_enrich_view`.

**Evidence.** 267 tests pass (4 new in tests/test_tick_harness.py: parse +
unify (perp 123.45 + spot USDC 10.55 → portfolio 134.0, both /info calls
address the configured user, raw payloads preserved by identity); spot outage
→ spot_usdc 0 / portfolio == perp / no raise; perp outage → httpx.HTTPError
propagates; null payload + malformed withdrawable → all-zero AccountState).
Ruff clean. Live-fire (real API, scratch DB, paper mode): `hlbot femr_tick`
risk-cap line derived through the new path (perp $23.22 + spot USDC $307.12 →
unified $330.34, 5×→$1652 / 1×→$330), manual NEAR position parsed from
`account.clearinghouse`, 6 decisions gathered, PAPER MODE — no orders.

**Honest caveats.** (1) Pure refactor + tests: no behavior change intended;
the two hardenings above are the only deltas (both convert a crash into a
zero, and a zero account value can only tighten caps). (2) `run_tick` does
not yet consume `AccountState` — full end-to-end unification still pending
the remaining slices.

**What's next (loop).** B12 remainder (overrides loading → roster →
`_enrich_view` move into runtime), B-EDGE2b re-confirm as the 15m store span
grows, B1c remaining hypotheses if idle. B-G014 still blocked: store 1m span
checked this iteration = 3.6d (needs ≥14d, ETA ~2026-06-26).

## Iteration 45 — 2026-06-12 — B12(h): overrides + roster construction into the tested tick harness

**What.** The auto-tuner overrides load, the agent-roster literal, and the
live-state filter — the last *logic* in the `femr_tick` preamble besides
`_enrich_view` — moved from `cli/main.py` into `agents/runtime.py`:

- `load_agent_overrides(path=None)` reads `configs/agent_overrides.json`
  (default path now shared with the auto-tuner command via `CONFIG_DIR`
  instead of a duplicated `parents[3]` walk). **Every** failure mode degrades
  to `{}` = built-in defaults, with a warning: missing/unreadable file,
  malformed JSON, non-object top level, or a non-object per-agent entry
  (dropped individually). Two of those used to crash the tick: a JSON array
  top level passed `json.loads` then died at `overrides.get` (AttributeError)
  during roster build, and a string-valued agent entry died at `dict.update`.
  Degrading is the right direction here: the defaults are the long-running
  tested baseline and the hard risk caps (compute_notional_cap /
  apply_allocator_caps) clamp sizing downstream whichever config wins.
- `build_roster(conn, overrides)` is the canonical 6-agent roster (femr,
  twap_mr, twap_mr_regime, liq_cascade, basis, breakout incl. the B-EDGE2a
  validated 15m Donchian config) with the per-agent defaults + override merge
  that previously lived inline as `_cfg`.
- `filter_live_agents` is `_filter_live_agents_by_state` relocated verbatim
  (CLI copy deleted; tests/test_live_agent_state.py imports updated).

`femr_tick` now does `agents = build_roster(conn, load_agent_overrides())` —
the preamble's remaining untested piece is just the `_enrich_view` pipeline.

**Why.** B12/REVIEW M3: the roster IS the trading system's configuration —
which strategies run and at what size — and it lived in an untested CLI
function, drifting one copy at a time. Now one tested function owns it, which
is also the prerequisite for `run_tick` (paper) consuming the same roster as
`femr_tick` (live) in the final B12 slice.

**Evidence.** 274 tests pass (7 new in tests/test_tick_harness.py: missing/
valid/malformed/array-top-level/garbage-entry overrides loading incl. the two
crash regressions; roster names+validated defaults incl. closes_15m_bars=385
sizing off the breakout entry; override merge applies without bleeding into
other agents). Ruff clean. Live-fire (real API, scratch DB, paper mode):
full 6-agent roster decided (8 decisions incl. femr XMR short, breakout XPL
long via the 385-bar 15m feed), PAPER MODE, no orders; the real production
`configs/agent_overrides.json` parses through the new loader byte-identical
to the old inline code (femr stop_loss_pct=0.0225 + twap_mr tuned params
merged over defaults, verified by direct comparison).

**Honest caveats.** (1) Behavior-preserving refactor except the two
crash→default hardenings noted above (both convert an aborted tick into a
defaults-run tick; sizing stays clamped by the risk caps either way).
(2) The paper `tick` command still builds its own small roster (veto +
funding_arb) — unifying it onto `build_roster` is part of the remaining
`_enrich_view` slice, not done here.

**What's next (loop).** B12 final slice (`_enrich_view` → runtime, then
`run_tick`/`femr_tick` one path end-to-end), B-EDGE2b re-confirm as the 15m
store grows, B1c remaining hypotheses if idle. B-G014 still blocked: store
1m span checked this iteration = 3.55d, newest bar ~2h old (top-ups healthy);
needs ≥14d, ETA ~2026-06-23..26.

## Iteration 46 — 2026-06-12 — B12(i, final): the view pipeline moves into the tested tick harness; B12 done

**What.** The last untested logic in the `femr_tick` preamble — `_enrich_view`
— moved from `cli/main.py` into `agents/runtime.py`, and the whole view
construction is now one tested function both paths consume:

- `runtime.enrich_view` is `_enrich_view` moved verbatim (diffed against HEAD:
  only the name, a type hint, and the httpx alias changed — VWAP/σ math, spot
  scaling, per-coin error isolation, 15m feed all byte-identical).
- `runtime.build_tick_view(base_url, agents, vwap_window=0, env=None)` →
  `TickView(view, vwap_window, bars_15m, ws)` composes the pipeline: REST
  universe fetch (`fetch_market_view`) → enrichment (window resolved CLI >
  `HLBOT_VWAP_WINDOW` env > 60; 15m bars sized by `closes_15m_bars(agents)`)
  → opt-in fresh-WS overlay (`HLBOT_WS_SNAPSHOT` → `overlay_ws_snapshot`,
  the only real liquidations feed).
- `femr_tick` (live) calls `build_tick_view` and keeps only the console
  summary; `run_tick` (paper `tick` command) switched from bare
  `fetch_market_view` to the same `build_tick_view` — paper decisions are now
  made on live-identical view inputs (VWAP/σ, spot mids, 15m feed, WS overlay).
- Stale module docstring in `runtime.py` rewritten (it still claimed "live
  order placement is intentionally NOT wired yet" — `execute_decisions` has
  owned it since B12a).

**Why.** B12/REVIEW M3 closes out: the `femr_tick` preamble now contains zero
untested logic — roster, overrides, account/risk state, view, ownership,
reconcile, decision gathering, and execution all live in `agents/runtime.py`
with unit tests, and the paper and live paths share the view pipeline
end-to-end. A divergence bug class (live sees a feed paper doesn't) is now
structurally impossible for the view.

**Evidence.** 276 tests pass (2 new: `build_tick_view` composition — env
window drives the candle fetch span, roster sizes the 15m feed, REST-only ⇒
`liquidations_feed=False`, no `HLBOT_WS_SNAPSHOT` ⇒ no overlay; WS overlay —
fresh snapshot file overlays mids/book_top and enables the real liq feed,
stale snapshot is ignored and REST stays truth). Existing enrich tests
re-pointed at `runtime.enrich_view`. Ruff clean. Live-fire (real API, scratch
DB, paper): `femr_tick` full 6-agent roster, summary derived from `TickView`
(vwap w=60, closes15m 20 coins ≤385 bars), 8 decisions incl. femr XMR short +
twap_mr BTC/NEAR shorts, PAPER MODE no orders; `hlbot tick` (run_tick path)
ran through the same pipeline, 739 decisions logged (whole-universe veto holds
— pre-existing behavior, `fetch_market_view` always returned all coins).

**Honest caveats.** (1) `run_tick` now makes ~21 extra REST calls per tick
(enrichment) — it's a manual paper command, nothing in deploy/ calls it.
(2) The `tick` command still runs its own small roster (veto + funding_arb);
unifying it onto `build_roster` needs paper-position synthesis + a log_holds
policy call (veto's output IS hold rows) → split out as B12j, low priority.
(3) Live-fire showed `spot: []` — today's HL spot payload doesn't pass the
±5%-of-perp sanity check; the logic is byte-identical to HEAD so this is
real-world API behavior, not a regression from the move (basis_v1 then holds,
which is its safe default).

**What's next (loop).** B-G014 still blocked: store 1m span this iteration =
3.6d after top-up (needs ≥14d; store started 2026-06-12, ETA ~2026-06-23..26).
Meanwhile: B-EDGE2b re-confirm as the 15m store grows, B1c remaining
hypotheses (beta-neutral xfund cross-section, lower cadence), B14a deploy
automation gaps, or B12j if idle.

---

## Iteration 47 — 2026-06-12 — B1c CLOSED: carry edge hunt ends; two measurement-integrity fixes it surfaced

**Context.** B-G014 (top priority) still blocked on store span (1m = ~3.6d,
needs ≥14d, ETA ~2026-06-23). Highest unblocked P0: the two remaining B1c
hypotheses on xfund_carry — (b) beta-neutral cross-section and (c) lower
cadence. Discovery while orienting: the `beta_neutral` lever (inverse-rolling-
beta leg sizing, tightening-only) was ALREADY implemented + unit-tested in an
interrupted Jun-8 session (`auto: commit changes from Claude session`,
6a530aa) but the real-data A/B was never run or recorded. This iteration ran
the full experiment matrix and closed B1c.

**Experiment matrix (xfund_carry_v1, trailing-90d window as of 2026-06-12,
10-coin ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,TRX,ZEC, maker numbers).**

| arm | maker bps | net$ | trades | win | sharpe | maxDD |
|---|---|---|---|---|---|---|
| 1h baseline | +11.3 | +2.14 | 76 | 55% | +1.21 | −0.2% |
| 1h beta_neutral (floor .5) | +12.9 | +1.56 | 72 | 56% | +2.01 | −0.1% |
| 1h beta_neutral (floor .25) | +11.9 | +1.06 | 48 | 67% | +2.31 | −0.1% |
| 4h baseline (honest funding) | −7.1 | −0.57 | 32 | 50% | −0.34 | −0.3% |
| 4h beta_neutral | −18.6 | −1.14 | 32 | 50% | −1.07 | −0.2% |
| 1d baseline (honest funding) | +177.1 | +6.26 | **14** | 67% | +2.12 | −0.1% |

Walk-forward (`hlbot confirm --prefer maker`, 70/30 split):
- 1h baseline: **FAIL** — IS −43.7bps (10 trades) / OOS +19.6bps (66).
- 1h beta_neutral: **FAIL** — IS −19.0bps (10) / OOS +17.3bps (62).
- 1d baseline: printed **"✅ CONFIRMED" on 2 in-sample trades** → false
  positive, see fix (b) below; correctly FAILS under the new trade floor.

**Findings.**
1. **Meta:** the 1h baseline swung −4.3bps (Iters 20/22/23, window ending
   ~Jun 8) → +11.3bps maker on a ~4-day window roll. ALL the profit lives in
   the recent June funding-dispersion pocket (walk-forward IS is deeply
   negative in every arm; eligible-leg counts collapse in the early window —
   only 10 IS trades). Sample variance ≫ signal: no full-sample positive from
   this family is meaningful without the walk-forward gate.
2. **Hypothesis (b) — beta-neutral sizing: a real VARIANCE lever, not an edge
   lever.** Monotone in shrink depth: Sharpe +1.21→+2.01→+2.31, maxDD halves,
   win% 55→67%, exactly as the dollar-neutral≠market-neutral thesis predicts.
   But it shrinks notional (net$ +2.14→+1.06) and cannot change the carry's
   sign across regimes — walk-forward still FAILS. Verdict: keep default OFF
   (evidence-before-capital); it's the right default to flip IF xfund ever
   clears G0 on durable evidence. Lever stays in the code, tested.
3. **Hypothesis (c) — lower cadence: PRUNED.** 4h is strictly worse than 1h
   (−7.1 vs +11.3 same-window maker; beta combo −18.6) — consistent with
   Iter 23: hourly rotation into the *highest*-funding names IS the carry
   engine, and a 4h decision clock holds stale rank picks. 1d is
   *unprovable, permanently*: 14 trades/90d by construction (top_k=2, and
   funding-history API retention caps the sample at ~90d → years to reach
   n≥100). The +177bps full-sample 1d print is a regime-pocket artifact on
   nothing — 30 of 91 frames are also warmup-dead, so it trades only the
   last ~61d.
4. **B1c CLOSED.** All five levers explored across Iters 20–23 + 47 (tighter
   entry, wider universe, churn cut, beta-neutral, cadence) are pruned or
   sign-preserving. Carry stays evidence-gated OFF; the agents remain in the
   repo for a future funding regime, with their levers tested.

**Changed (code — 2 measurement-integrity fixes the experiments surfaced).**
- **`backtest/data.py`** — coarse bars (>1h) now SUM the actual hourly funding
  settlements inside each bar (`fund_sum` over rows in `(ts−bar, ts]`) instead
  of extrapolating the last sampled rate ×4/×24, which paid an extreme print
  for a full bar while real funding mean-reverts within hours — flattering
  exactly the carry strategies coarse backtests exist to test. ≤1h paths
  byte-identical (per-bar pro-rating unchanged); `funding_hourly` still the
  raw last rate. 4h/1d caches refetched post-fix (4h: −6.3→−7.1 — the old
  method WAS overstating carry; 1d: +173→+177, i.e. the 1d mirage was regime
  concentration, not accrual error — both now honest).
- **`backtest/confirm.py` + `cli/main.py`** — G0 verdict gained a per-split
  trade floor: `min_trades` (default 20, `--min-trades`) on BOTH in-sample
  and out-of-sample; failures print "sample too thin to judge". Tightening-
  only. Prior recorded PASSes clear it (twap_mr 1m: 844 trades; breakout
  15m: 322 — OOS ≈ 30% ≫ 20/split; B-EDGE2b's next rerun re-checks under the
  floor regardless).

**Evidence.** 276 → **278 tests pass** (new: coarse-bar settlement summation —
in-bar sum with no pre-bar leak, zero-settlement bar accrues 0, funding_hourly
unscaled; thin-sample confirm FAILS at the default floor on a positive-edge
fixture with an explicit too-thin reason; existing discrimination tests pinned
at `min_trades=2`). `ruff check src tests scripts` clean. All matrix numbers
reproducible via `--config`/`--interval` flags on today's refreshed caches
(4h/1d refetched after the funding fix; one HL 429 mid-refresh, retried OK).

**Honest caveats.** (1) The 1h full-sample +11.3/+12.9bps maker prints are
real numbers on real data but walk-forward shows they're one June pocket —
do NOT read them as edge. (2) The funding-integration fix changes only
4h/1d backtests; no published number used those intervals (the Iter-20/22/23
carry numbers were 1h and stand). (3) `min_trades=20` is a judgment call,
not a statistics theorem; it's deliberately below every legitimate pass on
record and above anything noise has produced. (4) beta_neutral's Sharpe gain
is same-window — it has NOT been tested across regimes (moot while the
strategy itself fails the gate).

**What's next (loop).** B-G014 when store 1m span ≥14d (~Jun 23–26; keep
verifying top-ups). Meanwhile: B-EDGE2b breakout re-confirm as the 15m store
grows (now under the trade floor), B12j vestigial `tick` unification, or B14a
deploy-automation gaps. P0 edge hunting shifts fully to the momentum/
mean-reversion families — carry is closed.

## Iteration 48 — 2026-06-12 — B-EDGE2d: breakout_v1 fails out-of-universe — the 52d edge is regime+universe pockets, not a general property

**What.** B-G014 still blocked (1m store ~3.7d, needs ≥14d). Highest-leverage
unblocked P0: breakout_v1's G0 PASS (Iter 35) was measured only on the 10-coin
universe inherited from the *carry* hunt — never on coins it wasn't tuned on.
Ran the breadth test today via the API's ~52d 15m retention: same validated
config (lb=384/ex=96, w=385, 15m) on 10 fresh liquid coins —
CRV,ENA,LIT,NEAR,SUI,TON,WLD,XMR,XPL,XRP (top fresh by 24h volume, every one
verified to carry the full ~52d of 15m history; universe checked before
results were seen).

**Results (taker = honest arm; maker similar throughout).**
- Fresh universe full sample: +9.8bps taker / +12.1 maker, 434 trades, win
  40%, Sharpe +1.36, maxDD −18.5% — positive but far below the original
  universe's +36.4.
- Fresh universe walk-forward: **❌ FAIL** — IS +37.4bps (266 tr, sh +4.35),
  OOS **−31.5bps** (172 tr, sh −2.91). The full-sample positive is entirely
  the early window. Maker arm fails identically (OOS −33.8).
- Control: original ADA…ZEC universe re-confirmed from today's store
  (5018 frames, 52.3d): **✅ PASS** — IS +20.1 (226 tr) / OOS +70.4bps
  (96 tr), robust to taker-3×. Clears the new min_trades floor → this also
  completes B-EDGE2b's first scheduled rerun. So the breadth failure is the
  *universe*, not the window: same recent ~16 calendar days, original coins
  +70.4 OOS vs fresh coins −31.5.

**Per-coin attribution (single-coin engine runs, taker, same 70/30 split).**
Original-universe OOS gain is BROAD majors trend — ADA +154, SOL +118,
ETH +112, BTC +100, DOGE +67, TRX +60, ZEC +46, LINK +49, AVAX +15bps; only
HYPE −45. But original IS is mostly NEGATIVE per-coin (BTC −17, ETH −20,
ADA −31…) — IS was carried by ZEC (+156) and HYPE (+55). Fresh-universe OOS
bleed is broad mid-cap chop: NEAR −110, WLD −97, LIT −83, ENA −83, XMR −33,
XPL −32; only TON +33 / SUI +8 green.

**Interpretation (the finding).** breakout_v1's 52d evidence decomposes into
two pockets: (1) an early ZEC/HYPE trending pocket, (2) a recent synchronized
majors trend. Mid-cap narrative alts chopped with false breaks through the
same weeks. Donchian momentum on HL is therefore regime+universe dependent —
NOT a deploy-anywhere edge. The diversification thesis (corr ≈ −0.1 vs
twap_mr) still holds, and paper forward-testing continues, but the promotion
bar must now include a breadth arm: an edge that only exists on the coins it
was discovered on is curve-fitting with extra steps until proven otherwise.

**Changed (code).** Rolling retention destroys the breadth sample (~52d and
sliding), so future re-tests need their history preserved starting now:
- `backtest/store.py` — `BREADTH_COINS`/`BREADTH_INTERVALS` constants (the 10
  fresh coins, 15m only) + `harvest(extra_pairs=…)` sweeps individual
  (coin, interval) pairs outside the cross-product, deduped against it.
- `cli/main.py` — `harvest-candles` gains `--breadth-coins`/
  `--breadth-intervals` (defaults = the constants; `""` disables): breadth
  coins harvest at 15m ONLY so the per-run API load on the B-G014-critical
  1m harvest doesn't double. loop.sh/timer pick this up with zero changes.
- First harvest run: 40/40 pairs ok, breadth coins backfilled 52.1d × 5001
  bars each.

**Evidence.** 278 → **280 tests pass** (new: extra_pairs swept + deduped with
order pinned; CLI breadth defaults match the store constants and don't overlap
the main universe). `ruff check src tests scripts` clean. All numbers
reproducible: fresh-universe frames cached
(`CRV-…-XRP_15m_52d_w385.json.gz`), original universe from the store via
`--source store --days 0`, attribution via single-coin engine runs.

**Honest caveats.** (1) Single-coin attribution ≠ portfolio attribution — the
ranking/concurrency interactions differ; treat it as qualitative. (2) Picking
the fresh universe by *today's* volume has mild hindsight bias (today-liquid
correlates with recently-moved); it biases TOWARD finding momentum, which
makes the FAIL more damning, not less. (3) One 52d window: the breadth FAIL
is as regime-sampled as the original PASS — that symmetry is exactly the
point. (4) The fresh-frames API window ends ~2h before the store window;
immaterial at 4993 vs 5018 frames.

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26; top-ups
verified this iteration, 3.7d). B-EDGE2b reruns are now two-armed (original +
breadth universes, both under the min_trades floor). P0 edge hunt continues:
momentum family needs either a regime/universe selector (what *makes* a coin
trend-clean?) or a different family; breakout_v1 accrues paper evidence
meanwhile. B12j / B14a if idle.

## Iteration 49 — 2026-06-12 — B-EDGE2e: efficiency-ratio trend gate repairs the breadth failure; combined 20-coin book flips FAIL→G0 PASS

**What.** B-G014 still blocked (1m store ~3.7d). Picked up Iter 48's open
question — *what makes a coin trend-clean?* — and answered it with the
simplest standard measure: the Kaufman efficiency ratio (net displacement /
path length over a lookback; ~1 = clean trend, ~0 = chop). Shipped it as a
default-OFF entry gate on breakout (`min_efficiency_ratio`, `er_lookback_bars`,
pure `efficiency_ratio()` + filter in the candidate scan; unjudgeable history
blocks when the filter is ON), then A/B'd on real 15m/52.3d data across three
universes. Hypothesis: the breadth bleed (Iter 48) was false breaks out of
chop, which should score near-zero ER at entry.

**Results (taker = honest arm; lb=384/ex=96, w=385, 15m; baseline reproduced
exactly before sweeping: breadth IS +37.4 / OOS −31.5 FAIL).**
- Breadth universe (CRV…XRP, cached frames), ER over 96 bars (24h):
  er≥0.1 cuts only 48/434 trades but moves full-sample +9.8→+29.5bps and OOS
  −31.5→**−1.6** (sharpe +0.24) — the bleed is gone, still FAIL (<+3bps).
  er≥0.2 +13.5/OOS −30.5; er≥0.3 −29.7/OOS −71.3; er≥0.4 ~no trades. ER over
  384 bars is worse at every threshold (0.1: OOS −51.7; ≥0.2 kills the
  sample). So the filter's value is the *bottom tail*: only near-zero-ER
  breaks are reliably fake; higher cuts remove real trends.
- Original universe (ADA…ZEC, store, 5021 frames, 52.3d, 0 missing), er96≥0.1:
  still **✅ PASS** and stronger — taker +36.4→+39.2bps, OOS +70.4→**+88.6**
  (sharpe +6.34, 86 tr), trades 322→300. The threshold was selected on the
  breadth sweep, so this improvement is out-of-selection evidence.
- **Combined 20-coin book (both universes, store)** — the deploy-realistic
  test: baseline **❌ FAIL** (full +26.9bps/502 tr; OOS +3.1, sharpe +0.74);
  er96≥0.1 **✅ G0 PASS** — full taker **+43.9bps** (456 tr, sharpe +5.35,
  net +$401 on $1k), walk-forward IS +47.0 (306 tr) / OOS **+36.1bps**
  (sharpe +3.96, 156 tr), robust to taker-3× (+41.0). Dose-response: 0.05
  inert (cuts 2 trades, FAIL), 0.1 PASS (best net$), 0.15 PASS (+31.8 OOS),
  0.2 PASS (+40.7 OOS, fewer trades) — an effective band, not a knife-edge.

**Interpretation.** The ER gate is the universe selector Iter 48 asked for:
instead of hand-picking trend-clean coins (curve-fitting by universe choice),
deploy wide and let per-entry trend quality decide. Mechanically it both
skips false breaks AND frees the 5 concurrency slots + notional for
clean-trend names — which is why the combined book improves more than either
universe alone.

**Changed (code).**
- `agents/breakout.py` — `efficiency_ratio()` pure fn; `min_efficiency_ratio`
  (default 0.0 = OFF, exact pre-change behavior) + `er_lookback_bars` (96)
  config; entry-scan gate; ER in reasoning/market_snapshot when ON.
- `agents/runtime.py` — `breakout_er_v1` roster entry: same channel config +
  er gate ON (0.1/96), paper A/B arm beside the unfiltered breakout_v1
  (shared closes_15m feed, zero extra API traffic; live filter still drops
  both until human promotion).
- `configs/breakout_er_v1.yaml` — paper mode, same guardrails/promotion gates
  as breakout_v1 (promotion → live_small only, human-gated per B-PAPER3c).

**Evidence.** 280 → **286 tests pass** (ER math: trend=1/chop=0/zig-zag exact,
history + flat-path degenerates; decide(): filter blocks chop-break, admits
trend-break, default-OFF admits chop-break, unjudgeable-history blocks;
roster: er-arm config + feed sizing; config YAML loads paper-only). `ruff
check src tests scripts` clean. Live-fire: one real paper tick on a scratch
DB — breakout_er_v1 in roster, consumed the shared 15m feed, held correctly
(no current breaks; identical view to breakout_v1). All numbers reproducible:
breadth from `CRV-…-XRP_15m_52d_w385.json.gz` cache, original/combined via
`--source store --days 0`, e.g. `hlbot confirm --agent breakout_v1 --coins
<20 coins> --interval 15m --days 0 --vwap-window 385 --source store --prefer
taker --config '{"lookback_bars":384,"exit_lookback_bars":96,
"min_efficiency_ratio":0.1,"er_lookback_bars":96}'`.

**Honest caveats.** (1) The 0.1 threshold was picked from the breadth sweep on
this same 52d window — the combined-book PASS shares that data and is NOT
independent confirmation; the original-universe improvement is the only
out-of-selection signal, and it's same-calendar. (2) One 52d regime window,
same as every breakout number so far; the three-armed B-EDGE2b reruns on
longer store samples are the real test. (3) The book is chaotic at the
margin: the 0.05 arm cut 2 trades yet moved OOS by $12 (slot/cooldown
cascades) — treat per-arm deltas <$20 as noise; the 0.1 effect (+$100 OOS,
sign flip) is well above that. (4) Breadth alone still FAILS at every
threshold — the filter repairs, not creates, an edge; mid-caps have no
harvestable momentum here on their own. (5) Maker fills remain optimistic
for momentum; all promotion-relevant numbers quoted taker.

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26). B-EDGE2b
reruns now three-armed (original / breadth / ER-filtered combined). B-EDGE2f
(ER-arm correlation + paper A/B readout) once the paper books have ≥30d.
B12j / B14a if idle.

## Iteration 50 — 2026-06-12 — B-MAKERFILL: honest maker-fill model; the twap_mr maker case was a fill-assumption artifact

**What.** B-G014 still blocked (1m store ~3.7d). Closed the highest-leverage
honest-measurement gap instead: the backtester's maker mode filled every order
instantly at mid with maker fees — an admitted "optimistic upper bound" — yet
it was the entire evidence base for B-MAKER-LIVE ("maker +5.4 vs taker −0.0bps
at the live config") and for every maker-arm G0 number. Shipped
`CostModel(maker_fill="resting")` (+ `--maker-fill resting` on
`hlbot backtest`/`confirm`): a faithful replica of the live `--execution
maker` lifecycle (`exec/maker.py`) — entries rest as post-only limits at the
decision bar's mid, fill only when a LATER bar's mid trades strictly through
the limit (equality = unknowable queue position), stale quotes cancel after
1800s (live `DEFAULT_MAX_REST_S`), one working quote per coin (live
`has_resting_order`), fills land before `decide()` (live WS userFills
fold-in), and exits pay full taker fee+slip (the live proposal keeps exits
taker). Fill stats (rested/filled/expired) surface in results. Default
behavior byte-identical (`maker_fill="optimistic"`).

**Results (twap_mr_v1, 10-coin universe, store data, funding fetched).**
- **Exact live config (1m w=60, 3.7d, 5335 frames):** taker −1.2bps (1044 tr)
  · maker-optimistic **+4.2** (1042 tr, win 68%) · maker-rest **−4.5** (784
  tr, win 57%, 473 quotes / 83% filled / 80 expired). G0 confirm at
  prefer=maker-rest: **❌ FAIL** (IS −4.7 / OOS −3.6; the Iter-30 maker PASS
  was the optimistic model). Decomposition (counterfactual maker-priced
  exits): resting entries alone −1.7bps ⇒ **entry adverse selection ≈ −6bps,
  taker exit leg ≈ −2.8bps** of the optimistic→honest gap.
- **w=240 arm (B-WIN candidate, 1m, 3.7d):** taker +2.4 · optimistic +8.2 ·
  maker-rest **+0.3** (287 tr, 83% filled). The 4h window survives honest
  fills but its maker advantage over taker evaporates.
- **15m w=16, 52.3d (long sample):** taker −4.3 · optimistic +1.2 ·
  maker-rest **−13.9** (win 48%, 1930 quotes / 57% filled / 43% expired —
  the 1800s TTL is only 2 bars at 15m, and close-only fill detection is
  crudest at coarse bars; treat this arm as direction-only).

**Interpretation.** The mechanism is structural, not a window artifact: a
passive limit fading a deviation fills exactly when the deviation keeps
growing (the losers) and misses the instant reversions (the winners) — the
textbook adverse selection of maker execution for mean reversion. The
optimistic model assumed that selection away, which is why "maker" looked
like pure fee savings. REVIEW C1's "the taker tax is the structural bleed"
now has a corrected corollary: you cannot simply post your way out of it;
the spread you stop paying comes back as adverse selection.

**Honesty about bounds.** Resting mode is a PESSIMISTIC bound: close-only
mids miss intrabar wick fills, and those misses skew toward winners (price
touched the limit and reverted within the bar). Truth ∈ (−4.5, +4.2) at the
live config — the bracket straddles zero, so B-MAKER-LIVE is *unproven*, not
disproven. But the only positive number was the upper bound, so
evidence-before-capital flips the standing recommendation: do NOT enable
`--execution maker` live (deploy/README §Going-live updated; B-MAKER-LIVE
re-gated on maker-rest/B-FILL2 evidence; B-G014 maker arms must use
`--maker-fill resting`). Filed **B-FILL2** (intrabar h/l fill detection from
the store's candles) to tighten the bracket. Breakout promotion numbers are
unaffected (always quoted taker — the "maker fills optimistic for momentum"
caveat is now quantified). Carry pruning stands a fortiori (already FAIL on
the optimistic bound).

**Changed (code).** `backtest/engine.py`: `CostModel.maker_fill`
("optimistic"/"resting", validated) + `maker_ttl_s` + exit-leg cost
properties; `_Resting` book + `_process_resting` (stale-first, then
strict-cross fill at the limit); `_open` split into rest/fill paths
(`_fill_open` takes an explicit fill px; flip/average semantics unchanged);
`run()` processes quotes before decide; `BacktestResult.maker_fill_stats`.
`backtest/confirm.py`: `maker_fill` threaded through the gate (scenario named
`maker-rest`). `cli/main.py`: `--maker-fill` on backtest+confirm with early
validation + fill-stats print. deploy/README: going-live steps judge on taker
or maker-rest.

**Evidence.** 286 → **299 tests pass** (new `tests/test_maker_fill.py`, 13:
cross/no-cross/equality, TTL expiry incl. cancel-beats-same-bar-cross, one
quote per coin, taker exit pricing, fill-before-decide visibility, optimistic
default unchanged, taker mode ignores the flag, bad value rejected, confirm
threading). `ruff check src tests scripts` clean. All runs reproducible:
`hlbot backtest --agent twap_mr_v1 --coins ADA,…,ZEC --interval 1m
--vwap-window 60 --days 0 --source store --no-compare --maker --maker-fill
resting`; store topped up this iteration (40/40 pairs ok, 1m last bar <1h
old).

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26) — now
taker-judged with maker-rest arms. B-FILL2 (intrabar h/l fills) is the
natural next slice and also benefits breakout if a maker variant is ever
considered. B-EDGE2b three-armed reruns as the store grows. B12j / B14a if
idle.

## Iteration 51 — 2026-06-12 — B-FILL2: intrabar wick fills; close-only detection was adverse-selecting the fills themselves

**What.** B-G014 still blocked (1m store 3.7d, ETA ~Jun 23–26). Did the queued
next slice: the Iter-50 resting maker-fill model judged fills on close mids
only, which doesn't just *miss* fills — it selects for the bad ones (a quote
fills on close-through only when the move keeps going; the touch-and-revert
winners vanish). Store candles carry h/l, so: `Frame.highs/lows` (per-bar
intrabar extremes, populated by `build_frames` from candle `h`/`l`; rows with
missing/garbage h/l and legacy caches degrade silently to close-only),
`_process_resting` now fills a buy quote when the bar's LOW trades strictly
below the limit (sell: high strictly above; equality still no-fill — queue
position unknowable; TTL cancel-first unchanged), and a `filled_wick` stat
counts fills the close mid alone would have missed. The pre-B-FILL2 bound is
kept runnable as `--maker-fill resting-close` so the tightening is A/B-able
on identical data. Labels: `CostModel.exec_label` ("maker-rest"/"maker-restc")
shared by engine summary, confirm scenarios, and the CLI table.

**Results (twap_mr_v1, 10-coin store universe, funding fetched; same windows
as Iter 50 — the resting-close control reproduces Iter 50 EXACTLY: −4.5bps,
784 tr, 473 quotes/83%/80 expired).**
- **Exact live config (1m w=60, 3.7d, 5335 frames):** taker −1.2bps (1044 tr)
  · optimistic +4.2 (win 68%) · close-only rest −4.5 (win 57%) · **wick-aware
  rest +0.3bps** (976 tr, win 65%, 508 quotes / 96% filled / 19 expired —
  **226 of 489 fills (46%) were wick-only**). The honest bracket is now
  (+0.3, +4.2) — both ends ≥ 0 — vs (−4.5, +4.2) before. G0 confirm at
  prefer=maker-rest: **still ❌ FAIL** (IS +0.8/678 tr, OOS −0.7/262 tr,
  oos sharpe −3.39 — bar is +3bps/+1.0).
- **w=240 arm (1m, 3.7d):** taker +2.4 (346 tr) · **wick rest +4.7** (334 tr,
  98% filled, 85/168 fills wick-only) vs close-rest +0.3 / optimistic +8.2.
  **First config where maker beats taker on the pessimistic bound.**
- **15m w=16, 52.3d:** taker −4.3 · **wick rest −2.1** (3266 tr, 95% filled
  vs 57% close-only) vs close-rest −13.9 / optimistic +1.2. The anomalous
  "rest worse than taker" Iter-50 print is resolved as a detection artifact:
  at coarse bars almost every fill is intrabar, so close-only kept just the
  adverse tail.

**Interpretation.** Decomposing the Iter-50 "adverse selection ≈ −6bps": most
of it was the *model's* selection bias, not the market's — wick detection
recovers ~+4.8bps at the live config and ~+11.8bps at 15m. The remaining gap
to optimistic (+0.3 vs +4.2 at w=60) is the true adverse-selection +
taker-exit cost. Remaining model pessimism: an exact touch (low == limit)
never fills. Remaining optimism: the backtest posts at the decision bar's
close mid while live posts at the near touch (book-aware, ≤ mid for buys) —
fewer live fills at slightly better prices; direction ambiguous, small.
Verdict unchanged where it matters: **B-MAKER-LIVE stays evidence-blocked**
(G0 FAIL at the live config; all 1m numbers are one 3.7d window), but the
B-G014 multi-week maker-rest arms — especially w=240, where maker already
beats taker — are now judged with a bound tight enough to trust for a
PROMOTION, not just rejections.

**Changed (code).** `backtest/engine.py`: `Frame.highs/lows` (default-empty —
legacy caches load fine), `CostModel` `maker_fill="resting-close"` +
`wick_fills`/`exec_label` properties, `_process_resting` wick-through fill +
`filled_wick` stat, summary prints wick count. `backtest/data.py`:
`build_frames` carries candle h/l onto frames (skips rows with l>h or
missing). `backtest/confirm.py`: scenario labels via `exec_label`
("maker-restc" arm). `cli/main.py`: `--maker-fill resting-close` on
backtest/confirm, exec column + fill-note via `exec_label`, wick count in the
fill note.

**Evidence.** 299 → **308 tests pass** (9 new: wick fill buy/sell at the
limit px, exact-touch no-fill, close-cross not counted as wick, TTL beats
same-bar wick, resting-close ignores wicks + labels, confirm threads
resting-close, build_frames h/l carry + garbage-row skip, legacy-cache
load + fresh round-trip; 3 existing stats asserts gained the filled_wick
key). `ruff check src tests scripts` clean. All runs reproducible, e.g.
`hlbot backtest --agent twap_mr_v1 --coins ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,
SOL,TRX,ZEC --interval 1m --vwap-window 60 --days 0 --source store
--maker-fill resting` (store topped up by loop.sh, 1m last bar <1h old;
control via `--maker-fill resting-close`).

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26): taker-judged,
maker-rest arms now wick-aware — w=240 maker-rest is the arm to watch. After
that, the next dead measurement gap would be live maker-rest entry pricing at
the touch vs mid (only if a maker arm passes). B-EDGE2b three-armed reruns as
the 15m store grows. B-EDGE2f at ≥30d paper books (~Jul 8). B12j / B14a if
idle.

## Iteration 52 — 2026-06-12 — B12j: retire `hlbot tick`; veto becomes a read-only report

**What.** B-G014 still time-blocked (1m store needs ~14d, ETA ~Jun 23–26);
B-EDGE2b's three-armed rerun just happened (Iter 48–49) and B-EDGE2f needs
~Jul 8 paper books — so took the queued idle task. `hlbot tick` (the original
pre-B12 paper wrapper) still ran its own two-agent roster (VetoAgent +
FundingArbAgent — neither in `build_roster`) through `run_tick`. Scoping the
"unify" option found a reason to kill it instead: **since B-PAPER (Iter 37)
the paper book is real machinery**, and `hlbot tick` logged funding_arb_v1
paper `place` rows (BTC/ETH/SOL skeleton trades, no exit logic, never
maintained) straight into `agent_decisions` — those rows would replay into
`hlbot score --paper`, the track-record "Paper agents" section (B-PAPER3b),
and paper-evidence guardrails (B-PAPER3c) as if a real strategy were forward
testing. One stray manual `hlbot tick` = contaminated paper track record.
Unifying onto `build_roster` would just duplicate what `femr_tick` (paper
default — what deploy/run-tick.sh runs) already is.

**Changed.** (a) `cli/main.py`: `tick` command deleted; new read-only
`hlbot veto [--lookback-days/--min-trades/--threshold-bps]` replays the same
VetoAgent against `fills` and prints per-coin verdict/edge-bps/trades/net$
(worst first) while logging NOTHING — the advisory's actual value (which
coins the account bleeds on) with zero book pollution. (b) `runtime.run_tick`
deleted (its only caller was `tick`; `gather_decisions`/`build_tick_view`
remain the shared tested path under `femr_tick`); module/docstring
references updated. (c) Stale operator docs fixed: README quickstart + cron
example and INFRA.md now say `femr_tick`, so nobody reinstalls a cron on a
deleted command. (d) FundingArbAgent stays as the documented reference
skeleton (importable, config example), just no longer wired to any CLI.
Found & noted: `veto.current_vetoes` has zero callers anywhere — the
"other agents consult the latest veto rows" mechanism never grew a consumer;
kept as documented API, but it's dead weight if a future iteration wants it.

**Evidence.** 308 → **314 tests pass** (new `tests/test_veto_agent.py`, 6:
veto/allow/no-opinion verdict logic, lookback actually filters old fills +
widening it flips the verdict, both knobs configurable, advisory-only —
every action is `hold`; VetoAgent previously had zero direct tests). `ruff
check src tests scripts` clean. Live-fired on a scratch DB: seeded 25 losing
ZEC / 25 winning BTC / 3 losing HYPE fills → table shows ZEC veto (−1050bps),
BTC allow (+950), HYPE no-opinion (n=3 < 20), and `agent_decisions` count
stays **0** after the run. `hlbot --help` shows `veto`, no `tick`.

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26): taker-judged,
maker-rest arms wick-aware, w=240 maker-rest the arm to watch. B-EDGE2b
three-armed reruns as the 15m store grows. B-EDGE2f at ≥30d paper books
(~Jul 8). Idle queue: B14a (deploy automation — check what's actually left
vs the Iter-5/6 deliverables), B16b/B-PROP/B17 capital-formation specs.

## Iteration 53 — 2026-06-12 — B-GATES: roadmap G1–G3 as code (+ B14a audit-closed)

**Why this.** All research tasks are time-blocked (B-G014 needs 1m store
span ≥14d, ~Jun 23–26; B-EDGE2b just reran Iters 48–49; B-EDGE2f needs ≥30d
paper books, ~Jul 8), so took the idle queue. First audited B14a per Iter 52's
note: it is **fully delivered** — `deploy/` has the idempotent EC2 install,
test-gated auto-update, tick/report/ws/update/harvest systemd timers,
Litestream DB replication, AWS Terraform, and loop setup, no secrets in-repo.
The unticked P2 entry was a stale duplicate of the Iter-5/6 Done item; ticked,
nothing built. Then spent the iteration on a real measurement gap the audit
trail surfaced.

**The gap.** ROADMAP §4 gates capital on an evidence ladder (G1 paper → G2
live-small → G3 track record) and the mission says every iteration should move
a gate closer — but nothing in the repo could *answer where an agent stands*.
The YAML promotion gates cover only paper→live_small, and leave three holes:
(1) they check 30d-**window** scorecards, so a hot 5-day book can pass on
recency alone (nothing verified calendar evidence span); (2) breach **history**
is invisible — only a currently-failing guardrail blocks promotion, a pause
last week doesn't; (3) G2/G3 (live net incl. funding, DD, Sharpe stability)
existed only as prose. With two breakout paper arms maturing toward G1 and
twap_mr_v1 live accruing G2 evidence, the operator's "is it ready?" question
had no auditable answer.

**Changed.** (a) `supervisor/gates.py`: pure evaluators over existing
machinery (paper-replay/fills scorecards, book spans, `goal_evaluations`) —
`evaluate_g1` (paper span ≥30d, 30d edge ≥+5bps, ≥150 trades, 0 guardrail-fail
rows in 30d), `evaluate_g2` (live span ≥30d, net>0 incl. attributed funding,
maxDD<10%), `evaluate_g3` (≥60d, sharpe(all)≥1 AND sharpe(30d)≥0 — the
stability bar pre-declared conservatively — maxDD<10%),
`evaluate_roadmap_gates` composition. Evidence span = first→last row/fill (a
dead loop doesn't age into a pass); an N/A metric blocks as *unknown*, never
passes (missing `capital:` ⇒ DD unknown). (b) read-only `hlbot gates
[--agent] [--no-funding]`: per-agent per-gate verdict + named blockers,
modeled paper funding by default, footer states G0 = `hlbot confirm` and that
a PASS is operator evidence, never an auto-promotion. Nothing here mutates
state. (c) Stale operator doc fixed: deploy/README §Going-live cited Iter-50
maker numbers (−4.5bps lower bound) that Iter 51's wick-aware model
superseded (+0.3bps, G0 still FAIL — direction unchanged, numbers now
honest); added `hlbot gates` to the operate block.

**Evidence.** 314 → **332 tests pass** (18 new in `tests/test_gates.py`:
span helpers ignore live rows / return None without evidence; breach counting
filters guardrail-fail rows + since_ms; G1 pass on a mature profitable book,
blocked by short-span-with-strong-window-card (the exact hole), thin sample,
negative edge, recent-but-not-stale breaches; G2 pass with capital, unknown-DD
blocks without capital, negative net + fee drag; G3 pass on steady 61d,
short-span block, 30d-stability check catches a 25d recent collapse after 60
good days; composition by evidence). `ruff check src tests scripts` clean.
Live-fired on a scratch DB (3 seeded agents + configs roster): breakout_er_v1
G1 2/4 ("spans 17.6d need ≥30; trades 100 need ≥150"), breakout_v1 G1 0/4
(incl. the seeded breach), twap_mr_v1 (live_small via agent_state) G2 2/3 +
G3 2/4, no-evidence config'd agents render dim, `--agent` filter works.

**Found.** twap_mr_v1's G2/G3 DD check reads unknown-blocked: no `capital:`
in its YAML (breakout configs likewise). Filed B-GATES2 — bases should match
the real book caps and be operator-checkable, not guessed silently.

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch) — judge the winner's readiness with `hlbot
gates` + confirm together. B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8) — `hlbot gates` now shows the
countdown blockers directly. Idle queue: B-GATES2 (capital bases),
B16b/B-PROP/B17 capital-formation specs.

## Iteration 54 — 2026-06-12 — B-GATES2: capital bases unblock the G2/G3 drawdown checks

**Why this.** Research tasks still time-blocked (B-G014 needs 1m store span
≥14d, ~Jun 23–26; B-EDGE2b reran Iters 48–49; B-EDGE2f needs ≥30d paper
books, ~Jul 8). Top idle item was B-GATES2, filed by Iter 53's own live-fire:
`hlbot gates` showed twap_mr_v1's G2/G3 maxDD check unknown-blocked because
no evidence-bearing config sets `capital:` — fractional DD is N/A without a
base, and an unknown blocks the gate (correctly, but forever).

**Decision — bases = the agent's max deployable book, derived not guessed:**
- `twap_mr_v1: capital: 600` — $200/trade × 3 max_concurrent_positions from
  configs/agent_overrides.json (max_total_notional unset there, so
  concurrency binds).
- `breakout_v1` / `breakout_er_v1: capital: 60` — the $60 max_total_notional
  in their build_roster entries.
Each YAML carries the derivation as a comment. femr_v1 deliberately skipped:
all-time negative, no active promotion path; its base would be $20
(min($40 total, $20×1 concurrent) — concurrency binds, not the $40), worth
adding only if it ever earns re-evaluation.

**Guarding the "wrong base = misleading DD%" risk.** New
`test_capital_bases_match_roster_book_caps`: builds the real roster with the
committed agent_overrides.json and asserts every YAML `capital:` whose agent
is in the roster equals min(max_total_notional, per_trade × concurrency) —
so a future cap bump that forgets the YAML fails CI instead of silently
shrinking reported DD%. Plus an explicit-values test so *removing* a
`capital:` line also fails (the derivation test skips absent ones).

**Evidence.** 332 → **334 tests pass**, ruff clean. Live-fired on scratch
DBs (40d seeded twap_mr_v1 live book, agent_state live_small): with a −$20
dip the G2 DD check resolves to 3.3% of the $600 base → **G2 PASS** (was
unknown-blocked), G3 blocks only on span (39d < 60d); with a −$70 dip the
gate blocks with the named number "maxDD −11.1% (need better than −10%)" —
11.1% not 11.7% because DD is measured from the equity *peak* (+$30 accrued
pre-dip), the correct semantics. Both directions work; no state mutated
(`gates` is read-only).

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch) — `hlbot gates` + confirm judge the winner
together, DD checks now live. B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B16b/B-PROP/B17
capital-formation specs, B-SCALE doc once G2 evidence is real.

## Iteration 55 — 2026-06-12 — B16b: vault retargeting — the CAPITAL.md instruction would have traded the wrong account

**Why this.** Research tasks remain time-blocked (B-G014 needs 1m store span
≥14d, ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8). Top idle item was
B16b (vault launch checklist + bot retargeting, CAPITAL.md Track A — the
destination G3 exists for). Scoping it surfaced a real latent bug, which made
it a code task, not a doc task.

**The bug.** CAPITAL.md step 5 said "point the bot's `HL_TRADER_ADDRESS` at
the vault and let it trade." With the current code that is silently wrong:
`build_exchange` constructs `Exchange(account_address=…)` with NO
`vault_address`, and in SDK 0.23 the `vaultAddress` rides in the action
signature and the /exchange payload — `account_address` only redirects reads
(and `market_close` lookups prefer `vault_address` when set). Following the
doc at vault launch would have sized positions off the VAULT's state while
every order executed on the PERSONAL account. Worst-case: depositors' vault
shows no trades, personal account accumulates unmonitored positions sized
for someone else's equity.

**Changed.** (a) `config.resolve_vault_address()` — single source of truth
for `HL_VAULT_ADDRESS`; malformed values RAISE ("refusing to fall back to
the personal account") instead of degrading, because the failure mode this
guards is the operator believing the vault is live while orders land
elsewhere. `Settings.from_env().hl_address` prefers it (→ fills/funding/
equity ingest, reports read the vault book). (b) `exec/orders.py`:
`HL_VAULT_ADDRESS` constant, `_resolve_trader_address()` prefers the vault
(→ guardrail capital, account fetch, open-order/reconcile reads), and
`build_exchange` passes `vault_address=` to the SDK Exchange + logs it.
(c) `ops/doctor.py` grew a `vault` check (unset → "trading the personal
account"; set → shows the retarget; malformed → crit without raising, for
programmatic runs — the CLI fails earlier and louder via Settings).
(d) `scripts/daily_scorecard.py` env chain prefers the vault.
(e) Docs: CAPITAL.md step 5 corrected; GO_LIVE.md §Vault retargeting
checklist (gate on G3 + published record, verify HL fee/API-wallet terms,
flatten-first before the switch, watched first fill must land on the vault,
rollback steps); deploy/env.example documents the var as human-gated.

**Evidence.** 334 → **352 tests pass** (18 new in `tests/test_vault.py`:
resolution unset/blank/valid/5 malformed shapes, trader-address precedence +
malformed-raises-never-falls-back, Settings read-address preference,
build_exchange passes vault_address through to a fake Exchange (and None by
default), doctor check all three states). `ruff check src tests scripts`
clean. Live-fired `hlbot doctor` in all three states: unset → "✓ vault: not
set — trading the personal account"; valid → hl_address line shows the vault
+ "retargeted: orders sign vaultAddress=…"; malformed → loud ValueError, bot
refuses to run. Env unset ⇒ behavior byte-identical (no live change; the
var's use is human-gated behind G3 per GO_LIVE.md).

**Found.** `hlbot ws` never passes `user_address` to `run_ws`, so the
deployed WS service never subscribes userFills and B10b's instant maker-fill
detection is dormant (falls back to REST polling). Filed as B10c (P2,
one-liner + test; pass the vault-aware trader address).

**What's next (loop).** B-G014 when 1m span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B10c (ws
userFills), B-PROP / B17 capital-formation specs, B-SCALE doc once G2
evidence is real.

## Iteration 56 — 2026-06-12 — B10c: the deployed WS service now watches its own fills

**Why this.** Research tasks remain time-blocked (B-G014 needs 1m store span
≥14d, ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8). Top idle item was
B10c, filed by Iter 55: `run_ws(user_address=)` has existed since B10b
(Iter 19), but the CLI `ws` command never passed it — so every deployed WS
service ran without the userFills subscription and B10b's instant maker-fill
detection (`MarketState.user_fills` → `ingest_ws_user_fills` →
`reconcile_maker_fills` same-tick) has been dormant the whole time, silently
degrading maker fill detection to next-REST-poll latency. That latency gap
is exactly what B10b was built to close, and it matters the day B-MAKER-LIVE
ever flips.

**Changed.** (a) `exec/orders.py`: `_resolve_trader_address` promoted to
public `resolve_trader_address` — it now has a second caller, and the
vault-aware chain (HL_VAULT_ADDRESS > HL_TRADER_ADDRESS > HL_ADDRESS >
legacy default) must stay single-sourced; resolved at call time, not import
time. (b) `cli/main.py ws`: resolves the trader address and passes
`user_address=` to `run_ws`; the startup line now prints
`+ userFills[0x…]` so journald shows which account the service watches.
Vault semantics follow B16b for free: when a vault is live, fills land on
the vault, and the subscription follows it.

**Evidence.** 352 → **354 tests pass** (2 new in `tests/test_ws.py`: CLI
wiring via typer CliRunner with `run_ws` faked — personal-address arm
asserts HL_TRADER_ADDRESS reaches `user_address`, vault arm asserts
HL_VAULT_ADDRESS wins), `ruff check src tests scripts` clean. Live-fired
against the real socket (scratch DB + snapshot): startup prints
`subscribing ['BTC'] + userFills[0x5C3a…]` (legacy default — no env on this
box, the expected chain), websocket connects, no subscription error, written
snapshot carries the `user_fills` key. No deploy change needed: the systemd
unit's EnvironmentFile already supplies the address env.

**Found.** `hlbot ws --seconds N` never exits: the duration loop returns but
the SDK `Info(skip_ws=False)` websocket thread is non-daemon and never
disconnected (verified: exit 124 under `timeout 30` with `--seconds 5`).
Irrelevant to the forever-running systemd service; annoying for scripted
smoke tests. Filed as B10d (one-liner: `info.disconnect_websocket()` after
the loop).

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B10d (ws clean
exit), B-PROP / B17 capital-formation specs, B-SCALE doc once G2 evidence
is real.

## Iteration 57 — 2026-06-12 — B10d: bounded `hlbot ws` runs exit cleanly

**Why this.** Research tasks remain time-blocked (B-G014 needs 1m store span
≥14d, ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8). Top idle item was
B10d, filed by Iter 56's live-fire: `run_ws` finishes its `--seconds N`
duration loop but the SDK `Info(skip_ws=False)` websocket thread is
non-daemon and was never disconnected, so the process hung until killed
(exit 124 under `timeout`). Irrelevant to the forever-running systemd
service; it breaks scripted smoke tests — exactly the kind of check a
go-live runbook or CI canary wants to run.

**Changed.** `ingest/ws.py run_ws`: everything after `Info()` construction
(subscribes + duration loop) now sits in try/finally with
`info.disconnect_websocket()` in the finally — so the ws thread is torn down
on duration exit, on exception, and on Ctrl-C alike. The forever-running
service path (`duration_s=None`) is behaviorally unchanged (the loop never
returns; the finally only runs when something ends it, which is when you
want the disconnect anyway). Side effect: `run_ws` lost its
`pragma: no cover — requires a live socket` excuse — with `Info` faked it is
now unit-testable, so the subscription wiring (allMids + userFills-when-
address + per-coin l2Book/trades/activeAssetCtx) is pinned by a test for the
first time, not just eyeballed in journald.

**Evidence.** 354 → **356 tests pass** (2 new in `tests/test_ws.py`:
duration-exit arm asserts `disconnect_websocket` called AND the exact
subscription set for one coin + user address; raising-subscribe arm asserts
the finally still disconnects). `ruff check src tests scripts` clean.
Live-fired against the real socket (scratch DB + snapshot): `hlbot ws
--coins BTC --seconds 5` under `timeout 30` → websocket connects, snapshot
written (13KB), **exit 0 in ~6s** (was exit 124 at 30s).

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B-PROP / B17
capital-formation specs, B-SCALE doc once G2 evidence is real.

## Iteration 58 — 2026-06-12 — B-PROP: prop-eval rules as code + prep checklist

**Why this.** Research tasks remain time-blocked (B-G014 needs 1m store span
≥14d — measured 3.7d today, ETA ~Jun 23–26; B-EDGE2f needs ≥30d paper books,
~Jul 8; checked the local DB while orienting — it's empty, as expected: paper
evidence accumulates on the deploy box, not here). Top unblocked backlog item
was B-PROP (P3, CAPITAL.md Track B): prep for trading firm capital through a
funded-account eval. Scoping it surfaced a real machinery gap, so the
"checklist" got an operational core: prop evals breach on **equity (incl.
unrealized) from a day boundary** and on a **trailing-HWM max drawdown** —
both invisible to our existing account guardrail (`check_guardrails` is
rolling-24h and realized-only). Without code for those rules we cannot know
whether our own live curve would survive a given firm's eval before paying
the fee.

**Changed.** (a) `risk/prop.py`: `EvalProfile` (daily-loss % with
start-balance or day-open base + configurable UTC reset hour;
trailing/static max-DD; profit target; min trading days) +
`simulate_eval(profile, points)` — pure replay of any (ts_ms, equity) series
producing breach episodes (collapsed per day per rule / per excursion),
first-breach FAIL verdict (conservative: a breach after the target date
still fails — a funded account lives under the same rules), current headroom
to both floors, and observation-density stats (sampled-curve honesty: a real
eval marks continuously, so the report names its own blind spot). DB helpers
`equity_points` / `fill_trading_days`. (b) `hlbot prop-check`: read-only CLI
over `equity_snapshots`, all rule numbers as flags (defaults are loudly
labeled placeholders), breach table + headroom + verdict + density caveat.
(c) `docs/PROP_EVAL.md`: the B-PROP checklist — hard gate (no eval fee
before live G1+ evidence AND a ≥30d breach-free prop-check replay),
verify-terms table for Hypernova/Propr/Velotrade, rule→bot mapping
(`GuardrailConfig.max_daily_loss` ≤ 50% of the firm's daily allowance to
carry the realized-vs-equity gap; tightened notional caps; same strategy,
same params — "if the eval needs different params, we have a lottery
ticket"), isolation wiring (separate wallet/DB/services), in-eval abort
discipline, funded/failed exit rules. (d) CAPITAL.md Track B links the
checklist + tool.

**Evidence.** 356 → **368 tests pass** (12 new in `tests/test_prop_eval.py`:
the killer case — intraday unrealized dip below the daily floor on a day
that closes green → FAIL; same $ loss split across the reset boundary → no
breach vs same-day → breach; start vs day-open daily base; trailing vs
static DD divergence on the same curve; DD episode collapse + re-entry;
target-without-min-days stays IN_PROGRESS; headroom/density math; boundary-
hour shift; empty curve → NO_DATA; DB helpers; CLI smoke on a scratch DB
both FAIL and clean arms). `ruff check src tests scripts` clean. Live-fired
on a synthetic 10d hourly curve with one −4.6% intraday wick: prop-check
flags exactly that wick as a daily_loss breach (day closed green — the case
the realized-only guardrail can't see), 24.1 obs/day density line, verdict
FAIL; loosening to −10% daily prints "no breaches". No live behavior change
anywhere — the module is read-only analysis.

**Found.** `simulate_eval` accepts any equity series, and the backtest
engine already builds per-bar equity — wiring those together would pre-screen
a strategy against a firm's rules at bar resolution before any live capital.
Filed as B-PROP2 (small slice).

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B-PROP2 (backtest
curve pre-screen), B17 moonshot sleeve spec, B-SCALE doc once G2 evidence
is real.

## Iteration 59 — 2026-06-12 — B-PROP2: prop-eval pre-screen on backtest equity curves

**Why this.** Research tasks remain time-blocked (B-G014 needs 1m store span
≥14d, ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8). Top idle item was
B-PROP2, filed by Iter 58: `simulate_eval` accepts any (ts_ms, equity)
series and the backtest engine already builds per-bar equity, but nothing
joined them — so a strategy could only be screened against a prop firm's
rules from live `equity_snapshots`, i.e. after risking capital, at 15-min
sampling. Bar-resolution screening closes that gap for free.

**Changed.** (a) `risk/prop.py`: `parse_eval_profile(spec, start_balance=)`
— `--prop-profile` JSON → `EvalProfile` with parse_agent_config's
philosophy (malformed JSON / non-object / unknown key / wrong type /
out-of-range value = hard error, never a silent default screen;
`start_balance` is rejected as a JSON key — the screen's base IS the
backtest's `--starting-capital`, so percentage rules can't quietly sit on a
different base than the curve) + `EvalReport.summary()` (one-line
FAIL/PASS/IN_PROGRESS verdict with breach counts + first-breach detail, for
embedding in other reports; prop-check's rich table stays the detailed
view). (b) `hlbot backtest --prop-profile '{json}'`: each exec mode's
equity curve replays through the profile after scoring (trading days from
the engine's simulated fills via `fill_trading_days` on the in-memory
conn), printing a rules line + per-mode `prop[taker]: …` verdicts
(markup=False — rich was eating `[taker]` as a style tag) + the
bar-close-sampling caveat. Informational only: a FAIL prints but exits 0;
G0 stays `hlbot confirm`. (c) PROP_EVAL.md Step 1 + tooling footnote
document the screen (set `--starting-capital` to the eval account size —
fixed-dollar notionals mean the percentage rules only make sense on the
right base).

**Evidence.** 368 → **372 tests pass** (4 new in `tests/test_prop_eval.py`:
profile parsing defaults/overrides incl. int-for-float coercion; 13
rejection arms incl. bool-as-number and the start_balance key; summary
lines for all four verdicts incl. breach counts and unmet-condition text;
CLI smoke with `_load_backtest_frames` faked — winner path PASSes both
exec modes, a short marked against by +31% FAILs the daily line at exit 0,
`{bad` exits 1 with "not valid JSON"). `ruff check src tests scripts`
clean. Live-fired offline on real store data (3 coins × 1m × 2d, 297
trades): `--prop-profile '{"max_daily_loss_pct":0.01,…}'` → taker arm
**FAIL — equity 987.17 ≤ day floor 987.47** on an intraday 2026-06-11
08:28 dip (a 30-cent miss only visible at bar resolution; the maker arm
clears it breach-free, IN_PROGRESS on target/min-days). Exactly the
realized-vs-equity blind spot B-PROP built the module for, now measurable
pre-capital.

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B17 moonshot
sleeve spec, B-SCALE doc once G2 evidence is real.

## Iteration 60 — 2026-06-12 — B17: moonshot sleeve spec + ring-fence as code

**Why this.** Research tasks remain time-blocked (measured the store this
iteration: 1m span 3.7d vs the ≥14d B-G014 needs, ETA ~Jun 23–26; 15m at
52.1d with B-EDGE2b's three-armed rerun just done Iters 48–49; paper books
accumulate on the deploy box, ~Jul 8 for B-EDGE2f). Top idle item was B17
(CAPITAL.md Track D): spec the ring-fenced, loss-bounded moonshot sleeve.
Same shape as Iter 58's B-PROP: the checklist gets an operational core,
because the sleeve's one engineering property — worst case bounded and
written down BEFORE any bet — is mechanically checkable against a real
account, and an unenforced ring-fence is the exact failure mode the sleeve
exists to contain (lottery impulses expressing themselves inside the core
account that carries the track record).

**Changed.** (a) `docs/MOONSHOT.md` — the B17 spec. Honest framing first:
the sleeve is NEGATIVE-EV with a fat right tail, and its real function is
containment (a budgeted, fenced, death-ruled outlet so the impulse never
touches the core book). Invariants table: hard cap = one written-down
tranche (≤1–2% of capital, top-ups invisible to code — the no-top-up
discipline is the operator's contract), isolated-margin-only (on HL,
isolated margin IS the defined-max-loss primitive; cross puts the whole
sleeve behind one bet), per-bet margin ≤25% of cap, ≤2 concurrent bets,
kill floor 25% (DEAD → flatten, sweep the stub, stand down ≥90d),
sweep-to-core ratchet above the cap (bank the tail — it's the only reason
the sleeve exists), sleeve address ∉ {HL_TRADER_ADDRESS, HL_ADDRESS,
HL_VAULT_ADDRESS}. Bet discipline (3-line pre-registration: thesis/
invalidation/max-loss; no averaging down; expected funding counts inside
the max-loss budget), refund rules (fresh decision, ≤1/quarter, never in
the death week — the sleeve-shaped "never chase"; two consecutive dead
tranches = re-evaluate the concept), measurement (out of the public track
record BY CONSTRUCTION — own wallet, bot DB never ingests it; in the
personal P&L always). Gates: funding is operator-only and junior to live
G1+ evidence; any future moonshot *agent* starts at G0; the loop never
funds/wires/trades it. (b) `risk/sleeve.py`: `SleeveConfig` (validated) /
`parse_sleeve_positions` (keeps `leverage.type`, which the tick-path parse
drops — isolated-vs-cross is what the loss bound stands on) /
`evaluate_sleeve` → violations + notes + status (NO_DATA / DEAD /
VIOLATIONS / OK; DEAD outranks violations — a dead sleeve's only legal
moves are flatten + stand down). (c) `hlbot sleeve-check`: read-only CLI
(`--hard-cap` required — no sensible default for "the most you can lose";
address via flag or HLBOT_SLEEVE_ADDRESS, hex-validated; core addresses
from resolve_trader_address + resolve_vault_address), prints rules/equity/
bets table/violations/status + the top-up honesty caveat. (d) CAPITAL.md
Track D links the spec + tool.

**Evidence.** 372 → **393 tests pass** (21 new in `tests/test_sleeve.py`:
config rejection arms; parse keeps leverage type + skips malformed/
coinless entries; clean-sleeve OK with committed/headroom math; cross-
margin violation; oversized-bet violation; bet-count violation; kill floor
→ DEAD outranking a live violation; profit-above-cap → sweep note not
violation; case-insensitive core-address breach; empty state → NO_DATA;
CLI arms: clean OK exit 0, violation print, missing/malformed address exit
1, core-collision flagged, NO_DATA exit 1). `ruff check src tests scripts`
clean. Live-fired read-only against the real API pointing at the core
trader address as a worst-case demo: fetched the account's actual book and
flagged BOTH expected violations — "ring-fence breach: sleeve address IS a
core account" and "VVV: cross-margin position — loss is not bounded by the
bet's posted margin" — status VIOLATIONS, exit clean, nothing traded.

**Found.** Nothing requiring action: the live account currently holds a
cross-margin VVV short (3x, $138 notional) — that's the bot's normal book
(HL default margin mode), only a violation *for a sleeve account*, which
this account is not.

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B-SCALE doc once
G2 evidence is real; P3 spec items are now all done (B15/B16/B-PROP/B17).

## Iteration 61 — 2026-06-12 — B-PAPER3d: mark open paper positions to market

**Why this.** Every headline item is time-blocked (1m store span ~3.7d vs
B-G014's ≥14d, ETA ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8), so
the leverage available is making the evidence that IS accumulating honest.
B-PAPER3 shipped the paper scorecard realized-only with "follow-ups below" —
funding got fixed (B-PAPER3a) but mark-to-market never got filed. That's a
real blind spot for exactly the agents the loop is forward-testing:
breakout holds run 48–96h, so a position deep underwater is invisible to
`score --paper`, the supervisor's paper guardrails, and any operator
reading G1 evidence — the card shows only realized round trips. The
B-EDGE2f paper A/B readout would have compared two cards that can both hide
open losers.

**Changed.** (a) `scoring/paper.py`: `MarkedPaperPosition` +
`mark_paper_positions(positions, mids, cost)` — marks each open paper
position at the caller-supplied mid net of modeled exit costs. Design
invariant (pinned by test): upnl == the `closed_pnl − fee` a
`replay_paper_fills` flatten at that mid would realize (exit crosses the
spread + pays taker fee; the entry's fee was already charged to the card at
place time), so card-realized + open-uPnL = the book's flattened-right-now
value and nothing double-counts when the position later closes. Missing/
non-positive mid → mark_px=None/upnl=None, never a guessed price. Cards
stay realized-only BY DESIGN — marks are reported beside, never folded in
(module docstring updated). (b) `hlbot score --paper`: fetches mids once
(`_fetch_mids`, one allMids call; failure warns + degrades to unmarked;
`--no-mark` opt-out), open-positions table grows mark_px/upnl columns,
title states the mark semantics, and a per-agent "Open paper uPnL (if
flattened now; NOT in the realized cards above)" summary line prints.

**Evidence.** 393 → **397 tests pass** (4 new in `test_paper_score.py`:
long/short zero-cost marks; the mark==flatten invariant under exaggerated
costs, both sides; missing/bad-mid unmarked with fields preserved; CLI
smoke — marked arm asserts the title, summary line and +19.8 uPnL against
faked mids, `--no-mark` arm stays "not marked to market" with no summary).
`ruff check src tests scripts` clean. Live-fired against the real allMids
API on a seeded scratch DB (BTC long + SOL short): both marks correct sign
and magnitude, summary line `breakout_v1 +46.40`; loop-box real DB has no
paper book (it lives on the deploy box) so the readout there is unchanged-
empty as expected.

**Found.** Filed B-PAPER3e: the track-record paper section (the
public-grade artifact) is still realized-only — add an open-uPnL line via
`mark_paper_positions` (same degrade pattern as `_fetch_paper_funding`),
keeping marks out of sharpe/DD math. Deliberately NOT folding marks into
supervisor guardrail metrics for now: pause/demote on unrealized dips needs
its own design (a mark is a point-in-time price, not evidence of a closed
loss) — revisit if an open paper loser ever sits past its strategy horizon.

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B-PAPER3e
(track-record open-uPnL), B-SCALE doc once G2 evidence is real.

## Iteration 62 — 2026-06-12 — B-PAPER3e: open-position uPnL in the track-record paper section

**Why this.** Headline items stay time-blocked (1m store span vs B-G014's
≥14d, ETA ~Jun 23–26; B-EDGE2f needs ≥30d paper books, ~Jul 8), so the
leverage is again honesty of the evidence that IS accumulating. Iter 61
gave `score --paper` mark-to-market but filed B-PAPER3e: the track record —
the PUBLIC-grade artifact a reader will judge G1 evidence on — still showed
realized-only paper cards, so a breakout arm sitting days underwater on an
open 48–96h hold would have presented a clean card in exactly the document
that's supposed to be un-flatterable.

**Changed.** (a) `reports/track_record.py`: `build_track_record`/`export`
grow `paper_mids=`; each paper agent gets `open_upnl` — the flatten-now
value of its open book via the same `mark_paper_positions` the scorecard
uses (exit crosses the spread + pays taker fee, so card-realized +
open-uPnL = flattened-now book value, nothing double-counts on close).
Honesty rules: a fully-closed book is exactly 0.0; if ANY open position
lacks a mid the field is None (rendered "—") — a partial sum is never
shown as if it covered the book. md/html paper tables grow an "open uPnL"
column; PAPER_NOTE states the mark semantics and that marks are never
folded into net/edge/sharpe(d)/maxDD$ (sharpe/DD math untouched).
(b) `hlbot track-record`: `--paper-mark/--no-paper-mark` (default on)
fetches mids via the existing `_fetch_mids` (one allMids call) ONLY when
some paper agent has an open position — zero network with a closed/absent
book, fetch failure degrades to unmarked with a warning, mirroring the
`_fetch_paper_funding` degrade pattern.

**Evidence.** 397 → **400 tests pass** (3 new in `test_track_record.py`:
marked arm pins open_upnl == the canonical mark_paper_positions sum AND
the +19.82 hand-computed value, closed-book 0.0, md+html column render,
and realized net unchanged by marks; unmarked arm pins None→"—" offline
and None on a PARTIALLY marked two-coin book; CLI arm pins zero fetches
for a closed book, one fetch + $+19.82 in the exported md with an open
position, and --no-paper-mark skipping the fetch with "—" in the
artifact). `ruff check src tests scripts` clean. Live-fired against real
allMids on a seeded scratch DB (closed BTC trip + open SOL long @150 +
open ETH short @2500): track record shows realized $+0.79 beside open
uPnL **$−74.72** — the exact clean-card-deep-underwater blind spot this
closes — and cross-checks against `score --paper` per-position marks
(ETH +8.20, SOL −82.92; penny drift = mid moved between the two calls).

**Found.** Nothing new filed. B-PAPER3 follow-up chain is now complete
(3a funding, 3b section, 3c goals, 3d scorecard marks, 3e track-record
marks); the paper evidence pipeline reads honestly end-to-end ahead of
the ~Jul 8 B-EDGE2f readout.

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 23–26; w=240
maker-rest the arm to watch). B-EDGE2b three-armed reruns as the 15m store
grows. B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue: B-SCALE doc once
G2 evidence is real.

## Iteration 63 — 2026-06-12 — B-M4: the auto-tuner can no longer loosen live risk on its own

**Why this.** Headline items stay time-blocked (B-G014 needs ≥14d of 1m store,
ETA ~Jun 26 — store spans verified healthy today: 3.7d@1m/17.6d@5m/52.3d@15m,
exactly retention since the store was born this morning in Iter 28; B-EDGE2f
needs ≥30d paper books, ~Jul 8). Sweeping REVIEW for unpicked findings, M4 was
the live-risk hole: `scripts/auto_tuner.py` (pre-loop, Hermes cron on the
operator box) auto-applies LLM parameter tweaks straight into
`configs/agent_overrides.json` — the file every live tick merges over agent
defaults. Its prompt rule 5 explicitly tells the model to LOOSEN entries when
an agent is "winning but trading rarely", sigma_enter could drop 50%, and a
standing approval let it raise twap_mr per-trade notional to $200 — all with
zero backtest evidence, while everything built since (research_strategies,
supervisor promotion, G0–G3) is propose-only/evidence-gated. An LLM hunch off
7d of realized fills silently loosening the live book contradicts both hard
rules ("evidence before capital", "risk changes are tightening-only").

**Changed.** (a) `RISK_DIRECTION` per-key table (+1 = higher is tighter:
entry bars, volume floor; −1 = lower is tighter: notional, hold hours, stop
size) + `classify_changes` partitions validated changes into strictly-
tightening vs loosening; ambiguous keys (exits, take_profit) and keys with no
current value are NEVER auto-applied; no-ops dropped. (b) `dispatch_changes`:
tightening → `apply_overrides` (live, as before); loosening →
`agent_overrides.tuner_proposed.json` (same mergeable `{overrides, note}`
shape as research_strategies' document, note says review against backtest
evidence). The standing approval is preserved explicitly:
`HLBOT_TUNER_APPLY_LOOSENING=1` restores pre-M4 auto-apply-everything.
(c) Telegram/stdout output and the JSONL log now split "applied
(risk-tightening)" from "proposed for review"; (d) tuner file targets
(`HLBOT_TUNER_OVERRIDES/PROPOSED/LOG`) env-overridable — which is what made
the script unit-testable at all. validate_proposal rails untouched.

**Evidence.** 400 → **405 tests pass** (new `tests/test_auto_tuner.py` loads
the standalone script by path: direction partition incl. ambiguous/no-current/
no-op cases; dispatch writes tightening live + loosening to the proposal doc
and never the live file; all-tightening run leaves no proposal file; env flag
restores old behavior; and the FIRST pins on the pre-existing rails — femr
cap breach, liq_cascade TWAP-only scale rejection, >50% swing, out-of-bounds
stop). `ruff check src tests scripts` clean. NOT live-fired end-to-end:
main() needs the operator-box DB + claude binary; the changed logic is fully
covered by the dispatch/classify tests, and the validate→dispatch seam is
exercised in test_validate_rails_unchanged_by_m4.

**Found.** B2 closed by audit (all remaining pieces shipped as B2b/B-book/
B10b/B10c/B-MAKERFILL/B-FILL2; live maker routing is B-MAKER-LIVE,
evidence-blocked). REVIEW M5 (basis spot scaling) remains the last unpicked
review finding — deliberately skipped: basis_v1 isn't in the live roster and
REVIEW itself ranks it lowest priority.

**What's next (loop).** B-G014 when 1m store span ≥14d (~Jun 26; judge maker
with --prefer maker --maker-fill resting; w=240 arm the one to watch).
B-EDGE2b three-armed reruns as the 15m store grows. B-EDGE2f at ≥30d paper
books (~Jul 8). Idle queue: B-SCALE doc once G2 evidence is real.

## Iteration 64 — 2026-06-12 — B-PREREG: the headline confirms are frozen as pre-registered, ripeness-gated experiments

**Why this.** Every explicit backlog item is done or time/evidence-blocked
(B-G014 needs ≥14d of 1m store, today 3.7d, ETA ~Jun 26; B-EDGE2f needs ≥30d
paper books, ~Jul 8; B-EDGE2b's rerun needs the 15m store to outgrow the 52d
window it was measured on), and every backtest number so far was produced
today — a rerun adds zero new data. The leverage left is evidence *integrity*
for the runs the book is waiting on: B-G014 and B-EDGE2b existed only as
prose + an ETA. Prose fails two ways when the sample finally ripens: the
arms get assembled in the moment — i.e. after peeking at early numbers,
exactly the forking-paths bias `hlbot confirm` was built to kill — and the
"is the store ripe yet?" check is re-derived by hand every iteration.

**Changed.** (a) `backtest/experiments.py`: `load_spec` (JSON spec → frozen
agent/universes/interval/arms/thresholds/decision rule; ANY unknown key,
bad prefer/maker_fill, dup arm name, or malformed universe is a hard error,
never a silent default — a mislabeled arm would poison recorded evidence);
`check_ripeness` (per-coin store spans over the FULL arm universe;
worst-coin governs, a missing series is unripe, not 0d); `run_experiment`
(one frames build per distinct (universe, vwap_window) — frames bake the
window in — each arm fed to `confirm_strategy` with its frozen knobs;
loaders injected, so orchestration is offline-testable). (b) `hlbot
experiment <spec> [--check-only|--force|--store-root]`: refuses to run an
unripe spec (exit 3); `--check-only` is the span readout the loop can run
each iteration instead of hand-checking ETAs; `--force` prints an explicit
"peek, NOT the pre-registered verdict" banner; output = per-arm confirm
summaries + verdict table + the frozen decision rule (informational only —
nothing auto-acts). `_load_backtest_frames` gained a `store_root`
pass-through (frames_from_store already took it). (c) Two specs committed
in `configs/experiments/`: **b_g014.json** — twap_mr_v1, 1m store, 10-coin
universe, days=0, min_span 14d, SIX arms (baseline / stop_loss 0.03 / w=240,
each × taker AND maker-rest; no stop+window combo — anti-synergistic per
Iter 33), decision rule frozen: flip-proposals need PASS + beat same-basis
baseline, maker claims count only from resting-fill arms, baseline FAIL ⇒
Iter-29's 3.5d PASS was the pocket, propose nothing. **b_edge2b.json** —
breakout_v1, 15m store, three taker arms (original / breadth / combined-ER
0.1), min_span 60d (~Jun 20: the first sample that outgrows the frozen 52d
window), decision rule notes the 0.1 threshold was selected on this same
52d data so only post-Jun-12 bars are out-of-selection.

**Evidence.** 405 → **425 tests pass** (20 new in `test_experiments.py`:
spec parse + 10-case typo-rejection parametrization; worst-coin/missing-coin
ripeness; runner grouping (base+stop share one frames build, w240 gets its
own) and untouched pass-through of prefer/maker_fill/config/thresholds/
periods_per_year; CI pins on the COMMITTED specs — agents known, decision
rule present, b_g014 maker arms all resting + exactly the three configs ×
two bases, b_edge2b taker-only/385-window/20-coin-ER arm; CLI unripe → exit
3 before any frame/network work, check-only both ways, bad spec → exit 1).
`ruff check src tests scripts` clean. Live-fired: both registered specs
against the real store — b_g014 NOT RIPE (min 3.7d < 14d, all 10 coins
listed), b_edge2b NOT RIPE (52.1d < 60d, breadth coins the binding ones) —
and a throwaway 2-coin/1d/two-arm smoke spec ran the FULL pipeline on real
candles + real funding fetch: taker and maker-rest arms both through
confirm (maker arm's ladder correctly labeled maker-rest), verdict table +
frozen decision printed, exit 0. Smoke numbers discarded by construction
(1d peek; both arms FAIL walk-forward, as expected from a half-day OOS
tail).

**Found.** Nothing new filed. Note for future spec authors: `min_span_days`
should be bumped after each B-EDGE2b rerun so the next run waits for
genuinely new data (recorded in the spec's decision text and the backlog).

**What's next (loop).** Each iteration: `uv run hlbot experiment
configs/experiments/b_g014.json --check-only` (and b_edge2b) replaces the
hand ETA check. b_edge2b ripens first (~Jun 20), b_g014 ~Jun 26 — both runs
are now one command with frozen arms. B-EDGE2f at ≥30d paper books (~Jul 8).
Idle queue: B-SCALE doc once G2 evidence is real.

## Iteration 65 — 2026-06-12 — B-HB: the dead-man switch now watches a real heartbeat

**Ripeness checks** (the per-iteration readout B-PREREG installed): b_g014
NOT RIPE (1m span 3.7d < 14d, ETA ~Jun 26); b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20). Candle store healthy and
topped up by loop.sh.

**Why this.** Every headline item is time/evidence-blocked, REVIEW is swept
(only M5 left, deliberately lowest-priority). Auditing the ops spine that
the whole waiting game depends on found a structural hole: `assess_health`'s
"is the bot alive?" check read `MAX(ts_ms)` from `agent_decisions` with a
15-min crit bar — but `femr_tick` runs `log_holds=False` (and has since
birth; the hold-logging paper `tick` loop the check was designed against was
retired in B12j), so decision rows appear only when an order/error happens.
Two failure modes, both live today: (1) a healthy book that simply doesn't
trade for 15 minutes reads DOWN → heartbeat ping withheld + Telegram alert →
false pages train the operator to mute the pager — a muted dead-man switch
is no switch; (2) an actually-dead tick loop is indistinguishable from a
quiet market, so the G1/G2 evidence accumulation (paper books, live track
record) could silently stop for days. Highest-leverage unblocked fix:
ops-trust is what makes the multi-week waits safe to wait out.

**Changed.** (a) Schema: `tick_heartbeats` (ts_ms, mode, agents, decisions)
— one row per COMPLETED tick; `CREATE TABLE IF NOT EXISTS` in the schema
script `_conn()` already runs every invocation, so deployed DBs migrate on
first tick after update. (b) `runtime.record_tick_heartbeat` (tested) called
at the END of both `femr_tick` paths — paper just before the early return,
live after the execution loop — so a tick that aborts mid-way (e.g. perp
account fetch failure, build_exchange failure) does NOT beat, which is
exactly the dead-man semantics; a live roster that empties (last agent
demoted) also stops beating → pages → correct, that deserves eyes.
(c) `assess_health`: tick check keys on heartbeats (crit > max_tick_age_s);
legacy DBs without the table fall back to the old decision-age check
DEMOTED to warn (it can't tell quiet from dead, so it must never page);
new `activity` check — loop beating but no decision row for
`max_decision_age_s` (default 3d) → warn, the first detector for "evidence
stalled while everything looks alive" (broken roster/feeds). Warn doesn't
page (only DOWN does, unchanged CLI semantics) but renders in every tick's
journal + `hlbot health`. CLI exposes `--max-decision-age-s`.

**Evidence.** 425 → **430 tests pass** (test_ops.py 7→12: quiet-book-is-
not-down [the regression this kills], stale-heartbeat-pages-despite-recent-
decisions, legacy-DB-warns-not-pages, stalled-activity-warns, heartbeat
write→health read round trip, and a CLI wiring pin that runs `femr_tick`
paper end-to-end with faked account/view/roster and asserts the
("paper",0,0) heartbeat row). `ruff check src tests scripts` clean.
Live-fired on a throwaway DB: one REAL paper tick (full roster, real API;
twap_mr placed a paper XPL short) → `health` shows `✓ tick: last tick 0.0
min ago` + `✓ activity`; aging the heartbeat 1h back (decisions left fresh)
→ `🔴 DOWN, ✗ tick: last tick 60.2 min ago`, exit 1, Telegram path invoked.
Both directions behave.

**Found.** The `activity` warn is deliberately page-free this iteration
(avoiding new false-page classes while the threshold calibrates); if 3d
proves reliably quiet-free on the deployed boxes, consider a crit tier at
~7d so a silent-stall eventually pages. Not filed as a task — revisit when
real paper-box data exists.

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ripens ~Jun 20 — FIRST; b_g014 ~Jun 26). B-EDGE2f at
≥30d paper books (~Jul 8). Idle queue: B-SCALE doc once G2 evidence is
real; REVIEW M5 (basis spot scaling) remains the last unpicked finding.

## Iteration 66 — 2026-06-12 — B-M5: the basis spot feed was silently dead; now correct, banded, and tested

**Ripeness checks** (per-iteration readout): b_g014 NOT RIPE (1m span 3.7d
< 14d, ETA ~Jun 26); b_edge2b NOT RIPE (15m span 52.1d < 60d, breadth coins
binding, ETA ~Jun 20). Store healthy.

**Why this.** Headline items are time/evidence-blocked. REVIEW M5 (basis
spot scaling) was the last unpicked review finding — twice deliberately
skipped as lowest-priority, but the Iter-46 live-fire note ("spot: []  —
real-world API behavior") said the feed wasn't fragile, it was DEAD, and
nobody had asked why. Grounding the question in the real API found two
compounding bugs plus a latent measurement-integrity hole, exactly the
class this loop exists to close.

**Root cause (empirical, from the live payload).** (1) `enrich_view`'s
inline parser did `zip(meta.universe, ctxs)` — but the arrays are NOT
aligned: live API today returns 305 universe rows vs 590 ctxs (delisted
pairs leave holes; first misalignment at index 71), so UBTC/USDC's "mid"
was actually some other pair's price (0.000068). (2) It then scaled by
`10**(base_weiDecimals - 8)` — but midPx is already USDC-quoted (@142 mid
63668.5 vs perp 63682.5, a true −2bps basis); the scaling mangled correct
prices ×100. (3) The only thing standing between that garbage and the
paper book was the sanity band — documented as ±5% in the comment, coded
as ±50% (`0.5 < ratio < 1.5`). Today the garbage misses even ±50%, so the
feed degrades to empty and basis_v1 has held since birth. But the failure
mode was live: any payload drift landing a mis-parsed mid within ±50% of
perp ⇒ phantom basis up to 50% on an agent that enters at 0.2% ⇒ max-size
junk entries written into the paper book the track record publishes.

**Changed.** `runtime.normalize_spot_mids` (pure): ctxs joined by their
`coin` field == universe row `name` (immune to array misalignment), midPx
used unscaled, adopted only within ±5% of the perp mid (band is the
documented one, now enforced; parameterized), wrapped-over-plain
preference kept, every malformed shape (payload/meta/tokens/universe/
ctx/midPx) degrades to {} — spot is an enrichment, never tick-fatal.
`enrich_view` keeps only the fetch + call. Constant `SPOT_SANITY_BAND`;
coin list single-sourced from `agents.basis.BASIS_COINS`.

**Evidence.** 430 → **435 tests pass** (5 new in test_tick_harness.py: the
misaligned-universe fixture modeled on the live payload — join-by-name +
unscaled midPx pinned in one assert that both old bugs fail; 5% band
rejects/adopts + no-perp-anchor never adopts + band=0 disables; wrapped
beats plain in both orderings; malformed-payload parametrization incl.
per-row garbage skipped not fatal; enrich_view wiring end-to-end through a
fake client). `ruff check src tests scripts` clean. Live-fired twice:
direct call adopts {BTC, ETH, SOL} at +3.8/+5.4/+5.8bps basis (perp vs
spot — realistic for an arbitraged market); a full paper `femr_tick` on a
scratch DB shows `spot: ['BTC', 'ETH', 'SOL']` (was `spot: []` in the
Iter-46 live-fire) and `basis_v1 hold :: no basis>20bps; current(bps):
{BTC 9.0, ETH 11.3, SOL 12.3}`.

**Found.** (a) basis_v1 now forward-tests for real on the deployed paper
box — first time ever; it enters only at >20bps divergence (live basis
runs 4–12bps, so entries should be rare events: dislocations). Its paper
card is per-agent-isolated, caps $25/trade $50 total, no promotion path —
REVIEW's "tiny edge, well-arbitraged" verdict stands until a paper card
says otherwise. (b) Meta-lesson recorded in REVIEW M5's fixed-note: a
"fragile" data path that yields empty-forever is a dead feed, and a dead
feed with a 10×-too-loose sanity band is a loaded gun. (c) REVIEW.md is
now fully swept — every finding fixed or explicitly dispositioned.

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST, then bump its min_span_days after the
run; b_g014 ~Jun 26). B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue:
B-SCALE doc once G2 evidence is real.

## Iteration 67 — 2026-06-12 — B-AGG: the aggregate 5× cap was an accident of roster size; now enforced

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20); b_g014 NOT RIPE (1m span
3.7d < 14d, ETA ~Jun 26). Store healthy (20/20 + 10/10 pairs reporting).

**Why this.** Headline items time-blocked; REVIEW fully swept. The next
structural milestone is B-SCALE — growing size via "the 5×/1× risk rule +
MetaAllocator" once G2 evidence lands — so this iteration audited that
exact spine. It held per-agent but not in aggregate.

**Root cause.** `risk/allocation.py`'s docstring promises a two-layer rule
(portfolio ≤ 5× unified value; each agent ≤ 1×), but `resolve_agent_caps`
never reads `risk_cap.max_total_notional` — only the per-agent 1× ceiling.
The 5× total entered the system solely as the MetaAllocator's
`total_capital`, which its own floors bypass: cold-start agents get
min_alloc ($50) and negative-Sharpe agents neg_floor ($25) BEFORE the
budget check, and step 3 hands every positive-Sharpe agent
max(min_alloc, share) even when nothing remains. After the step-4 clamp
each alloc is bounded by 1× portfolio, so the AGGREGATE invariant
Σ ≤ 5× held only while the roster had ≤5 agents — roster size, not code.
Worse, the breach is maximal exactly in a drawdown: a shrunken portfolio
puts every cold/floored agent AT its full 1× ceiling, so an N-agent roster
could carry N× portfolio. No downstream layer catches it (each agent's
`decide()` checks only its own cfg cap; there is no portfolio-level check
at order placement).

**Changed.** `resolve_agent_caps` grew the aggregate layer: after the
existing per-agent resolution, if Σ totals > `risk_cap.max_total_notional`,
every agent's total scales down by the same factor (allocator's relative
weights preserved) and per-trade clamps to min(old per-trade, new total) —
an explicit smaller configured per-trade is never touched. Under-cap books
return byte-identical (tightening-only, pinned by test); zero-portfolio
books short-circuit (no divide-by-zero). One enforcement point, in the
module already documented as the single source of truth — the
MetaAllocator's floors stay as-is (cold-start semantics are intentional;
the overshoot is now caught where the rule lives).

**Evidence.** 435 → **441 tests pass**; `ruff check src tests scripts`
clean. New: 5 in test_allocation.py (8 agents × 1× → Σ pinned to exactly
5×, proportionality at uneven weights 100/80/70 → 80/64/56 under a 0.8
scale, explicit $10 per-trade survives a halving while unconfigured
per-trade follows the total down, under-cap byte-identity, zero-portfolio
no-error) + 1 end-to-end in test_tick_harness.py (`apply_allocator_caps`
with 6 cold agents, $30 1× ceiling, $150 5× cap → six $25 cfg mutations,
Σ = $150.00). No live-fire needed: pure function, no I/O, and today's live
roster (≤5 agents) is in the byte-identical branch.

**Found.** (a) MetaAllocator.allocate can still SUGGEST a sum above its
total_capital (floors + step-3 min) — now harmless, the resolve layer is
the enforcement point; left deliberately. (b) MetaAllocator docstring says
fills are attributed by "agent name prefix" but the query is exact-match
`agent = ?` — actual attribution flows via the cloid convention to exact
names (ingest/hyperliquid.py), so the code is right and the comment stale;
not worth a commit on its own, noted here. (c) There is still no
portfolio-level notional check AT ORDER TIME (caps are applied pre-tick,
positions drift intra-hold); acceptable while per-trade sizes are small,
revisit if B-SCALE raises caps materially.

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST, then bump its min_span_days after the
run; b_g014 ~Jun 26). B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue:
B-SCALE doc once G2 evidence is real.

## Iteration 68 — 2026-06-12 — B-FUNDGR: the daily-loss halt was blind to funding

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20); b_g014 NOT RIPE (1m span
3.7d < 14d, ETA ~Jun 26). Store healthy (20/20 + 10/10 pairs reporting).

**Why this.** Headline items time-blocked; REVIEW swept. Iter 67's flagged
"no order-time portfolio check" was scoped and deliberately NOT built:
`check_guardrails` already blocks entries on the marked aggregate pre-tick,
per-agent caps bound the intra-tick add (post-B-AGG Σ caps ≤ 5×), so the
residual is second-order drift — Iter 67's own "revisit when B-SCALE raises
caps" stands. But auditing that same guardrail surfaced a first-order hole
in the layer that IS the hard stop.

**Root cause.** The 24h daily-loss measure summed `closed_pnl − fee` from
`fills` only. Funding is realized hourly cash flow on HL but lands in
`funding_payments`, not fills — so a book sitting against extreme funding
bleeds without printing a single fill and the halt never trips. Magnitude:
extreme HL funding ~0.1%/hr × 5× portfolio notional ≈ 12%/day vs the 3%/day
`dynamic_daily_loss_limit` — funding can DOMINATE the daily loss exactly in
the funding-extreme regime femr trades and breakout's multi-day holds sit
through. Second find while wiring it: `_coin_holders_over_time` (the
equal-split fallback behind funding attribution, used by live scorecards
too) read `agent_decisions` without an `is_paper` filter — a paper agent
"holding" a coin in its paper book could claim a share of REAL funding
whenever a coin had no fills or netted ~0.

**Changed.** (a) `scoring/metrics.py`: `_coin_holders_over_time` filters
`is_paper = 0` (paper rows never claim live cash); new public
`agents_funding_since(conn, agents, since_ms)` rolls up the existing
size-weighted attribution across the roster, deduped. (b)
`exec/orders.py check_guardrails`: 24h PnL = fills + min(0, attributed
funding) — clamped so a funding LOSS tightens the halt but funding income
never widens the loss headroom (symmetric inclusion would loosen the gate
vs fills-only; left as a documented operator call). Attribution failure
degrades to the fills-only measure with a log warning rather than aborting
the tick (a crash here would also skip the risk-reducing flattens). Both
halt and OK messages now show the funding term.

**Evidence.** 441 → **447 tests pass**; `ruff check src tests scripts`
clean. New: 4 in test_guardrails.py (funding-only bleed halts + message
names funding; fills −6 + funding −6 trips a $10 limit neither alone would;
+$50 funding cannot mask a −$11 fills breach; manual-coin funding ignored)
+ 2 in test_attribution.py (paper rows get no share while a live holder
over the same span gets the full payment — the pre-fix leak; rollup splits
3:1 by size, dedups repeated names, excludes manual). Live-fired
`agents_funding_since` against the real `data/hlbot.sqlite` (runs clean on
the production schema; 0 funding rows on the loop box — funding ingest
happens on the deployed box, unit tests carry the behavior).

**Found.** (a) Clamp is applied to the cross-agent NET funding (consistent
with fills netting across agents); per-agent clamping would be stricter —
revisit if a funding-collecting agent ever shares the live book with a
funding-paying one. (b) `check_guardrails` re-fetches user_state + spot
USDC although `femr_tick` already holds an `AccountState` — duplicate API
call, two reads could diverge within one tick; harmless today, fold into
B12-style consolidation if touched again. (c) Iter 67(c) (order-time
aggregate check) re-confirmed as second-order; stays parked behind B-SCALE.

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST, then bump its min_span_days after the
run; b_g014 ~Jun 26). B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue:
B-SCALE doc once G2 evidence is real.

## Iteration 69 — 2026-06-12 — B-GR1: guardrails judge the tick-start snapshot

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20); b_g014 NOT RIPE (1m span
3.7d < 14d, ETA ~Jun 26). Store healthy (20/20 + 10/10 pairs reporting).

**Why this.** Headline experiments time-blocked; REVIEW swept. Iter 68's
found-(b) was the top flagged item: `check_guardrails` re-fetched
user_state + spot USDC although `femr_tick` already holds the tick-start
`AccountState`.

**Root cause / risk.** Three problems in one seam. (1) Divergent truth:
the risk caps (`compute_notional_cap`, `dynamic_daily_loss_limit`) are
sized from the tick-start snapshot, but the halt verdict judged a second
fetch made seconds later — the two could disagree within one tick.
(2) Duplicate API calls: one extra user_state + one extra spot read per
live tick. (3) The sharpest one, found while making the change: the
mid-tick fetch was a CRASH point — `_retry` exhausting its 3 attempts
raises out of `check_guardrails`, and the call site has no try/except, so
a transient API blip between decision gathering and execution aborted the
tick INCLUDING the risk-reducing flattens (the exact failure mode B-FUNDGR
guarded against in the funding-attribution arm). With the snapshot
injected there is no mid-tick fetch to fail.

**Changed.** `check_guardrails` grew a keyword-only `account=` param
(duck-typed `agents.runtime.AccountState`; TYPE_CHECKING-only import keeps
the exec→agents dependency out): when provided, capital + assetPositions +
spot USDC come from the injected snapshot, no fetch; legacy fetch path
unchanged for snapshot-less callers (only caller is femr_tick, updated).
No snapshot AND no Info client fails SAFE — (False, "misconfigured")
halts new entries, never fail-open, never crash. Docstring documents the
remaining (pre-existing) race: a resting quote can fill between this check
and order placement; bounded by the pre-tick cap layer + per-order caps,
unchanged by this commit.

**Evidence.** 447 → **450 tests pass**; `ruff check src tests scripts`
clean. New in test_guardrails.py: (a) PoisonInfo (raises on any use)
proves the injected path never touches the network and Info becomes
optional (None gives the identical verdict); (b) fetched-vs-injected
verdict identity on the same numbers — a notional breach read through the
injected payload's assetPositions ($120 BTC vs $100 cap) and a capital-
floor breach (perp $10 + spot $5 < $40); (c) the fail-safe arm asserts
halt + "misconfigured". Tests construct the REAL AccountState dataclass so
the duck-typed field names are pinned by CI. No live-fire: the new path is
pure given its inputs, the legacy path is byte-identical code under an
`else`, and the changed call site is live-mode-only (human-gated).

**Found.** (a) `risk/prop.py` references check_guardrails in prose only —
no second caller to migrate. (b) femr_tick now performs exactly one
user_state + one spot read per tick, both at tick start; if anyone later
adds a third consumer of account truth, thread `AccountState` through
rather than fetching (this is the B12 pattern).

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST, then bump its min_span_days after the
run; b_g014 ~Jun 26). B-EDGE2f at ≥30d paper books (~Jul 8). Idle queue:
B-SCALE doc once G2 evidence is real; per-agent funding clamp if a
funding-collecting agent ever shares the live book with a funding-paying
one.

## Iteration 70 — 2026-06-12 — B-RIPE: gap-aware experiment ripeness

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20); b_g014 NOT RIPE (1m span
3.7d < 14d, ETA ~Jun 26). Store healthy — 0 missing bars on every pair
(the new readout would now say so if it weren't).

**Why this.** Headline experiments time-blocked; REVIEW swept. First
candidate investigated: does HL funding-history retention threaten the
upcoming 60–90d store-sourced backtests the way candle retention did
(funding is API-fetched at backtest time, no store)? **Pruned by probe**:
`fetch_funding_history` returns the full 200d requested for BTC, XPL, ZEC,
HYPE (4800 hourly rows each) — funding retention ≥200d, no funding store
needed. Second candidate was real: the pre-registered experiment gate has
a contiguity hole.

**Root cause.** `check_ripeness` judged store SPANS only. `coverage_of`
computes interval-aligned missing bars precisely so "a harvester outage
can't silently pass a holey sample as a full one" (B-HIST2) — but the
ripeness gate threw that field away (`CoinSpan` didn't even carry it).
The loop box harvests per-iteration, best-effort; 1m API retention is
~3.5d. If the loop stalls >3.5d during the next two weeks, the 1m store
gaps PERMANENTLY — yet b_g014 would still hit "span ≥14d", ripen, and the
frozen verdict would be recorded off a sample with a hole bar-count
windows silently straddle (VWAP/σ across the gap, funding accrual skipping
the hole). The run-time coverage print is a dim per-coin line under a
20-coin readout — disclosure, not a gate; exactly the in-the-moment
judgment call pre-registration exists to remove.

**Changed.** `backtest/experiments.py`: `CoinSpan` grows
`missing`/`missing_pct`; `ExperimentSpec` grows `max_missing_pct`
(default 1.0%, spec-overridable, typo-validated like every spec key);
`RipenessReport.ripe` now requires span ≥ min AND worst-coin missing_pct
≤ allowance; `summary()` names the binding gap ("span ok at Xd; COIN_1m
Y% bars missing > 1.0% allowed") and keeps disclosing sub-threshold gaps
("N missing = Z%") even when ripe. `check_ripeness` trims to the spec's
`days` window first (global last-bar anchor, matching `frames_from_store`)
so an out-of-window gap can't block a `days>0` spec forever; both frozen
specs are days=0 so their full store IS the judged sample. CLI help text
updated. Frozen spec JSONs untouched (byte-identical — the default
applies; no post-hoc spec edits).

**Evidence.** 450 → **453 tests pass**; ruff clean. New: gappy-store
block (3d span passes the 2d min but 2.3% missing > 1% → NOT RIPE, summary
names coin+gap; same hole diluted under 1% by a 15d store → RIPE with the
gap still disclosed); days-window trim (hole in day 1 of 4d store: days=2
spec RIPE on the clean window, days=0 spec blocked by the same hole);
spec-level allowance widening (5% passes what 1% blocks); both registered
specs pin `max_missing_pct == 1.0`. Live-fired both `--check-only`
readouts against the real store: verdicts unchanged (still span-blocked),
0 missing bars across all 30 pairs.

**Found.** (a) Funding retention ≥200d (probe above) — recorded so the
"harvest funding too" idea isn't re-explored. (b) DOGE/LINK/TRX show 5333
bars vs 5334 elsewhere with 0 missing — last-bar capture timing, not
holes; the coverage math distinguishes these correctly. (c) The backtest
CLI's own store path (`hlbot backtest --source store`) still only PRINTS
coverage (red >1%) without gating — acceptable for exploratory runs
(operator sees it interactively); the gate matters where the verdict is
pre-registered.

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST, then bump its min_span_days after the
run; b_g014 ~Jun 26) — both now also gap-gated. B-EDGE2f at ≥30d paper
books (~Jul 8). Idle queue: B-SCALE doc once G2 evidence is real;
per-agent funding clamp if a funding-collecting agent ever shares the
live book with a funding-paying one.

## Iteration 71 — 2026-06-12 — B-EXPREC: pre-registered verdicts persist as records

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.1d < 60d, breadth coins binding, ETA ~Jun 20); b_g014 NOT RIPE (1m span
3.7d < 14d, ETA ~Jun 26). Store healthy (0 missing bars, all 30 pairs) —
but found 6h stale at session start with /tmp/ralph_harvest.log empty
(loop.sh's pre-iteration top-up appears not to have run this session);
topped up manually via `hlbot harvest-candles` (all pairs ok). Harmless at
6h against 3.5d retention, but worth watching: if the per-iteration
guarantee is flaky, the 1m store gap risk B-RIPE gates against is live.

**Why this.** Both headline experiments time-blocked; picked the gap that
makes those runs more valuable. `hlbot experiment` printed its verdict and
exited — the single most evidence-bearing output in the program (it gates
B-MAKER-LIVE, B-SCALE, breakout promotion) would exist only in terminal
scrollback plus whatever PROGRESS prose the loop hand-transcribes.
Transcription re-opens the side channel pre-registration froze shut: an
arm dropped, a number rounded, a forced peek quietly unflagged. The honest
machinery deserved a durable, tamper-evident output.

**Changed.** `backtest/experiments.py`: `experiment_record` (pure builder —
spec identity incl. sha256 of the frozen file so a post-hoc spec edit
changes the hash; the ripeness readout the run happened under, gaps
included; `forced` honesty bit; injected code rev + timestamp; every arm's
RESOLVED knobs [coins/window inheritance applied] + full confirm numbers
incl. cost ladder) and `write_experiment_record` (collision-proof: peeks
get a visible `.peek` filename, same-second reruns suffix `-2`/`-3`, spec
names sanitized so a hostile name can't escape the dir). CLI: `hlbot
experiment` grows `--results-dir` (default `configs/experiments/results/`
— committed beside the specs; loop.sh's `git add -A` makes persistence
automatic even if an iteration forgets) and `--no-record`; recording is ON
by default for ripe runs AND forced peeks — peeking now leaves a permanent
trace, so "peek early, then present the ripened run as the first look" is
auditable. `_git_rev` (best-effort `git rev-parse HEAD` anchored at the
spec dir, degrades to null — the engine/fill-model rev flipped verdict
signs in Iters 50/51, so a record without it is reproducible only by
guesswork).

**Evidence.** 453 → **457 tests pass**; ruff clean. New: record builder
self-containment (resolved arm knobs, thresholds, ripeness spans, None-
sharpe/cost-ladder serialization, lossless JSON round-trip); writer
never-overwrite + `.peek` naming + hostile-name containment; CLI wiring
both directions (ripe run → recorded verdict w/ matching spec sha256 +
degrade-to-null code_rev in a non-repo tmp dir; unripe `--force` →
`.peek` file with `forced:true, ripe:false`; `--no-record` writes
nothing). `_git_rev` success path live-fired against the real repo
(returns HEAD c80938c). No real-spec run: a `--force` on b_g014/b_edge2b
would be a peek — deliberately not taken.

**Found.** (a) loop.sh harvest top-up didn't run before this session
(empty log, 6h-stale store) — single observation, no action beyond the
manual top-up; if it recurs, instrument loop.sh to date-stamp the log.
(b) The frozen specs stay byte-identical (sha256 now pinned into every
future record).

**What's next (loop).** Per-iteration: the two `--check-only` ripeness
readouts (b_edge2b ~Jun 20 FIRST — after it runs, commit the auto-written
results JSON and bump its min_span_days; b_g014 ~Jun 26). B-EDGE2f at
≥30d paper books (~Jul 8). Idle queue: B-SCALE doc once G2 evidence is
real; per-agent funding clamp if a funding-collecting agent ever shares
the live book with a funding-paying one.

## Iteration 72 — 2026-06-12 — B-EDGE3: cross-sectional momentum passes G0 at 14d (with caveats)

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.3d < 60d, ETA ~Jun 20); b_g014 NOT RIPE (1m span 3.9d < 14d, ETA
~Jun 26). Store healthy (0 missing bars) and fresh (topped up 11:43 UTC,
minutes before session start) — though /tmp/ralph_harvest.log is again
absent, so whatever refreshed the store wasn't loop.sh's logged top-up;
freshness is what matters and it held, but the logging gap means we can't
yet tell WHICH mechanism is keeping the store alive. Second observation;
still no action beyond noting it (the B-RIPE gap gate is the backstop).

**Why this.** Both headline experiments time-blocked; the open P0.6 umbrella
is the edge hunt (the book has one proven engine + one paper candidate —
single-strategy risk before any AUM). Carry pruned cross-sectional *funding*
ranking; the untested sibling is cross-sectional *return* ranking (xmom),
the standard market-neutral momentum factor, and a different machine from
breakout (relative ranks, ~market-neutral book vs per-coin channels,
net-directional book).

**Changed.** `agents/xmom.py`: `trailing_return` (pure; optional
`skip_bars` reversal guard) + `XMomAgent` — rank eligible coins (volume
floor) by trailing return over `lookback_bars`, long top_k / short bottom_k
(entries need ≥2×top_k ranked coins), exits by rank hysteresis (leave only
when out of the top/bottom `exit_rank` — xfund Iter-23 lesson: exact-rank
rotation churns a cross-sectional book to death) + stop/max-hold/no-signal,
re-entry cooldown like breakout. Registered in `_backtest_factories` as
`xmom_v1`. **Bundled hardening (found, needed to run at all):**
`backtest/data.py` `retry_rate_limited` — fetch_candles/fetch_funding_history
had NO 429 handling, so one rate-limited page aborted a whole 20-coin
history load with nothing cached (the hourly harvester shares this IP;
collisions are routine — two runs died before the fix). Now 429 retries
with 2..32s exponential backoff (other HTTP errors propagate untouched;
last attempt re-raises). This also protects the pre-registered experiment
runs, which fetch funding through the same path.

**Evidence.** 457 → **477 tests pass** (17 xmom: signal math incl. skip
guards, rank/entry/caps, both-side hysteresis, stop/max-hold/no-signal/
cooldown exits, engine integration long-winner-short-loser profits while
no-dispersion stays flat, factory registration; 3 retry: backoff-then-
succeed, non-429 propagates, exhaustion re-raises); ruff clean.

**Backtest numbers** (90d × 1h × 20 coins = original 10 + breadth 10, real
API history, walk-forward `hlbot confirm`, taker preferred — maker numbers
are the optimistic fill model, not relied on):
- lookback **336 (14d): G0 PASS** — IS +19.5bps/122tr/sharpe +0.70, OOS
  +131.7bps/80tr/+3.37, full-sample taker +67.0bps/194tr/sharpe +1.80/
  maxDD −2.1%, robust to taker-3× (+51.4). Gate note: sharpe≥1 is checked
  on OOS only (standing rule, `confirm.py`); IS sharpe 0.70 disclosed.
- lookback 168 (7d): FAIL — full-sample +4.0bps but IS −6.2 / OOS +20.4.
- lookback 72 (3d): FAIL the other way — IS +29.8 / OOS −41.9 (full +3.7).
  Opposite-sign flips across the same boundary = regime noise, not signal.
- skip_bars 24 on lb168: HURTS (taker +4.0 → −14.3) — recent-day momentum
  is part of the signal here; keep skip default 0.
- Sub-universes at lb336: original-10 **PASSES** (IS +9.2/OOS +107.0, taker
  +47.2 full, robust to 3×); breadth-10 alone **FAILS** (IS −10.0/OOS
  +12.9, full +3.7) — edge concentrates in the liquid majors set, same
  shape as breakout's B-EDGE2d breadth finding.
- Correlation vs breakout-ER on identical 1h frames (96h channel + ER 0.1):
  daily-PnL corr **−0.01** over 91 days — uncorrelated. (vs twap_mr not
  run: no comparable same-frame twap config at 1h — live twap is a 60×1m
  VWAP; the 15m-frames comparison goes with the B-EDGE3b rerun.)

**Honest read.** The 14d arm was selected after watching 7d/3d fail the
same window — same-window selection, exactly the bias that made breakout's
first PASS look stronger than it was. One 90d sample; the OOS window is a
momentum-friendly pocket (breakout earns its OOS in the same calendar
weeks). What survives skepticism: IS alone is +19.5bps on 122 trades, the
cost ladder barely dents it (3× taker −15.6bps off a +67 base, vs twap_mr
where 2× kills), maxDD −2.1% is small, and dose-response in horizon is
monotone (3d noise → 7d flat → 14d signal — consistent with the momentum
literature's intermediate-horizon sweet spot). NOT promotable on this
evidence; promotion path = B-EDGE3b pre-registered reruns as fresh data
arrives + B-EDGE3a paper forward-test, mirroring breakout's discipline.

**Found.** (a) The 429 fragility (fixed, above). (b) Harvest-log absence
recurring — see ripeness paragraph. (c) breakout-ER at 1h cadence on the
20-coin book prints +4.2bps/858tr on this window (side-output of the
correlate run; its validated config is 15m — context only, no action).

**What's next (loop).** Per-iteration: the two `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — after it runs, commit the auto-written record +
bump min_span_days; b_g014 ~Jun 26). Then: B-EDGE3a (closes_1h feed +
paper roster wiring) and B-EDGE3b (freeze b_edge3.json; decide 1h-harvest
vs 15m-bar-scaling for store-sourced reruns). B-EDGE2f at ≥30d paper books
(~Jul 8). Idle queue unchanged (B-SCALE doc on real G2 evidence; per-agent
funding clamp if mixed funding-sign agents ever share the live book).

## Iteration 73 — 2026-06-12 — B-EDGE3a: xmom_v1 paper wiring (1h closes feed + roster + goals YAML)

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.3d < 60d, ETA ~Jun 20); b_g014 NOT RIPE (1m span 3.9d < 14d, ETA
~Jun 26). Store healthy (0 missing bars on both spec universes).

**Why this.** Both headline experiments time-blocked; Iter 72's named next
step. xmom_v1 passed G0 on 90d×1h (taker +67bps, OOS +131.7) but with
same-window-selection caveats — the honest promotion path is a paper
forward test, and until this iteration the agent had no live-tick data
feed, no roster entry, and no goals config, so zero G1 evidence could
accumulate. Mirrors B-EDGE2a (breakout's paper wiring) exactly.

**Changed.**
- `agents/runtime.py`: `closes_1h_bars()` roster sizer — refactored the 15m
  sizer into a shared `_closes_feed_bars(agents, key)` (need = 1 +
  max(lookback+skip, exit_lookback), so it covers breakout's exit channel
  AND xmom's skip_bars with absent knobs reading 0); `enrich_view(...,
  closes_1h_bars=)` fetches genuinely-1h candles per top coin into
  `view.extra["closes_1h"]` (one call/coin, 337 rows ≪ 5000 cap; 0 = no
  traffic — live mode today, where the live filter drops unpromoted
  agents); `TickView.bars_1h`; `build_tick_view` sizes both feeds off the
  roster.
- Roster: `xmom_v1` entry with the validated config — lookback_bars 336,
  closes_key closes_1h, $20/leg, max_total $80, max_concurrent 4 (top_k=2
  ⇒ at most 4 legs, so the cap is real). skip_bars stays 0 (the A/B said
  it hurts), pinned by test.
- `configs/xmom_v1.yaml`: paper mode, capital 80 (= roster book cap, pinned
  by test_capital_bases_match_roster_book_caps), guardrails (24h −$15
  pause / 7d −20bps demote / 24h −60bps alert), promotion paper→live_small
  ONLY behind 30d gates set ≈ half backtest edge (edge ≥30bps, net ≥$5,
  ≥40 trades). The same-window-selection caveat is written into the YAML
  comment so the bar is on record.
- `cli/main.py`: tick summary prints the 1h feed (`closes1h: N coins (≤B
  bars)`).
- Bundled nit: xmom's idle hold message said "no xmom book" even while
  holding a fully-aligned book; now "book steady (N legs)" vs "no xmom
  book" (paper logs get read for weeks; no test pinned the old string).

**Evidence.** 477 → **480 tests pass**; ruff clean. New/extended:
closes_1h_bars roster scan (lookback+1, skip extends, 15m/1h sized
independently), enrich_view 1h fetch span + trailing-slice + zero-traffic
default, build_tick_view composes both feeds, roster pins (names order,
xmom knobs, both feed sizings), YAML loads paper-only + capital pin.
**Live-fired on the real API (scratch DB, 3 paper ticks):** summary shows
`closes1h: 20 coins (≤337 bars)`; xmom_v1 entered the full dollar-neutral
book — WLD +63.3%/LIT +42.7% long (ranks 1–2/16), SOL −17.9%/ZEC −17.9%
short (ranks 15–16/16), $20 each = $80 cap; next tick's replay showed all
four legs bot-owned and held (`book steady`), no duplicate entries.

**Found.** Nothing new beyond the hold-message nit (fixed above). Harvest
log situation unchanged from Iter 72 (store fresh, mechanism unlogged).

**What's next (loop).** Per-iteration: the two `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — after it runs, commit the auto-written record +
bump min_span_days; b_g014 ~Jun 26). Then B-EDGE3b: freeze b_edge3.json
(decide 1h-harvest vs 15m-bar-scaling for store-sourced reruns — adding
1h to harvest-candles lets reruns outgrow API retention like breakout's
breadth fix did). B-EDGE2f at ≥30d paper books (~Jul 8); xmom's paper
card hits 30d ~Jul 12. Idle queue unchanged (B-SCALE doc on real G2
evidence; per-agent funding clamp if mixed funding-sign agents ever share
the live book).

## Iteration 74 — 2026-06-12 — B-EDGE3b: 1h harvest + frozen xmom rerun spec — first verdict KILLS the promotion case

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.3d < 60d, ETA ~Jun 20); b_g014 NOT RIPE (1m span 3.9d < 14d, ETA
~Jun 26). Store healthy (0 missing bars on both spec universes); loop
harvest topped everything up this session (15m → 52.6d, 1m → 4.0d).

**Why this.** Iter 73's named next step. xmom_v1's G0 PASS (Iter 72) was
explicitly caveated as same-window-selected; the rerun spec is how that
claim gets adjudicated honestly. The B-EDGE3b blocker was a data decision:
the store had no 1h series (harvester collected 1m/5m/15m only).

**Decision: 1h-harvest over 15m-rescaling.** The validated config is
native 1h bars; rescaling to 15m (lb=1344) changes exit/stop evaluation
cadence — a different experiment, not a rerun. And 1h retention turned
out to be ~5000 bars ≈ **208d**, so the first harvest captures ~7 months
at once and reruns then outgrow API retention like breakout's breadth fix.

**Changed.**
- `backtest/store.py`: `DEFAULT_INTERVALS` + `BREADTH_INTERVALS` grow
  "1h" (both universes — the spec judges the combined 20-coin book).
  loop.sh and the systemd harvest timer pick it up automatically (both
  call with CLI defaults); `test_breadth_universe_cli_defaults_match_
  store_constants` now also pins the main `intervals` default against
  `DEFAULT_INTERVALS` (that drift hole was open) + pins "1h" in both.
- First 1h harvest live-fired: 18/20 coins at 208.3d (5001 bars), LIT
  171.9d, XMR 189.8d — zero missing bars, all contiguous.
- `configs/experiments/b_edge3.json` frozen: xmom_v1, 1h, source=store,
  days=0, vwap_window 337 (= lb+skip+1), three taker arms (combined-20 /
  original-10 / breadth-10, all lb336/skip0 — skip pinned because
  skip_bars=24 HURT in Iter 72), maker excluded by design. Decision rule
  discloses the overlap caveat up front: on a ~208d sample the 70/30
  walk-forward's OOS tail (~62d) lies INSIDE the Iter-72 selection window,
  so IS-on-extended-history (mostly pre-March, never touched) is the
  genuinely fresh evidence. Pin test `test_b_edge3_spec_pins` mirrors the
  b_edge2b one.

**The verdict (pre-registered, ripe, not forced — record
`configs/experiments/results/b_edge3.20260612T124331Z.json`):**
- combined-taker **FAIL**: IS **−11.8bps**/470tr/sharpe −0.59, OOS
  +51.4/166tr/+1.56; full-sample taker +4.2bps (Iter 72 printed +67.0 on
  90d). robust-to-2× True but the gate fails on IS sign.
- original-taker **FAIL**: IS −13.1/348tr, OOS +45.3/132tr/+1.88.
- breadth-taker **FAIL**: IS −27.4, OOS +4.4, full −15.2, not robust.

**Honest read.** The ~146d IS leg is mostly out-of-selection history and
xmom LOSES on it across every universe; the fat OOS tails are exactly the
already-seen June momentum pocket the 14d lookback was picked on. By the
spec's own frozen weighting, Iter 72's edge was the pocket, not the
strategy — **the promotion case is dead on this evidence.** This is the
pre-registration machinery doing precisely what it was built for, one
iteration after the candidate looked promotable. xmom_v1 stays a paper
agent (zero-risk out-of-time forward test — the cleanest arbiter if the
regime returns); the FAIL record now stands in front of its promotion
bar. min_span_days bumped 150→200 post-run per the frozen protocol (next
rerun ~Jul 10, when ~28d of post-selection data exists; the recorded
sha256 pins the spec as it ran).

**Evidence.** 480 → **481 tests pass**; ruff clean. Harvest + experiment
both live-fired on the real API (numbers above); verdict record committed
beside the specs.

**Found.** (a) HL 1h candle retention ≈ 208d (5000-bar pattern holds).
(b) LIT has only ~172d of 1h history (listed later) — it is the
min_span_days binding coin for b_edge3 ripeness.

**What's next (loop).** Per-iteration: the two `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — after it runs, commit the auto-written record +
bump min_span_days; b_g014 ~Jun 26; b_edge3 ~Jul 10). B-EDGE2f at ≥30d
paper books (~Jul 8); xmom's paper card hits 30d ~Jul 12 (still worth
reading even with the promotion case dead — forward test is out-of-time).
Idle queue unchanged (B-SCALE doc on real G2 evidence; per-agent funding
clamp if mixed funding-sign agents ever share the live book). The edge
hunt (P0.6) is back to two candidates: twap_mr (proven, live) +
breakout-ER (paper, b_edge2b pending).

## Iteration 75 — 2026-06-12 — B-EDGE2g: breakout extended-history read at 1h — all arms FAIL; momentum's profit is one pocket

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m span
52.4d < 60d, ETA ~Jun 20); b_g014 NOT RIPE (1m span 4.0d < 14d, ETA
~Jun 26); b_edge3 NOT RIPE (1h LIT 171.9d < 200d, ~Jul 10). Store healthy,
zero missing bars on all three universes.

**Why this.** With all three specs waiting on the calendar, the
highest-leverage unblocked move was making the calendar irrelevant for the
breakout question: Iter 74's 1h harvest captured ~208d, and every breakout
number in the book rests on ONE ~52d 15m window ending Jun 12 — the exact
sample shape whose extension killed xmom one iteration ago. Rescaling the
validated config to native 1h bars trades cadence for span and buys the
extended-history read ~3 months before the 15m store can provide it.

**Changed.**
- `configs/experiments/b_edge2_1h.json` frozen: breakout_v1, 1h,
  source=store, days=0, vwap_window 97 (=lb+1), three taker arms mirroring
  b_edge2b at the SAME time horizons (lb=96/ex=24/er_lb=24 at 1h = 96h
  channel / 24h exit / 24h ER, identical to lb=384/ex=96/er_lb=96 at 15m;
  stop/max-hold/cooldown are in hours and carry over unchanged). The frozen
  decision rule states up front that this CANNOT adjudicate breakout_er_v1's
  15m promotion case (4× coarser exit/stop evaluation = different
  experiment; b_edge2b stays the gate) and that IS-on-extended-history is
  the genuinely fresh evidence (everything before ~Apr 21 untouched; all
  parameters incl. ER 0.1 were selected on the 52d window, so the 70/30
  OOS tail lies inside the selection window — b_edge3's overlap caveat).
- `test_b_edge2_1h_spec_pins` mirrors the other spec pins (horizons,
  taker-only, ER knobs, the can't-adjudicate-15m sentence).

**The verdict (pre-registered, ripe at 171.9d worst / 0 gaps, not forced —
record `configs/experiments/results/b_edge2_1h.20260612T125649Z.json`):**
- original-taker **FAIL**: IS **−10.5bps**/994tr/sharpe −0.97, OOS +17.1/
  412tr/+2.09; full-sample taker −2.6bps/1406tr.
- breadth-taker **FAIL**: IS **−17.6**/1024tr, OOS +12.6/530tr; full −6.9.
- combined-er-taker (the promotion candidate's config) **FAIL**: IS
  **−13.9**/1294tr, OOS +16.3/620tr/+2.41; full-sample taker −4.0bps/
  1912tr. None robust to 2× slippage.

**Honest read.** The ~146d extended IS leg is decisively negative on every
universe at >1000 trades/arm — not thin-sample noise — while every positive
tail sits inside the already-seen Apr–Jun momentum pocket. This is the
exact xmom shape (Iter 74), now across two independent strategy families
(rank momentum + Donchian channel). Two honest limits, frozen in the spec
before the numbers existed: (a) 1h evaluation genuinely costs edge (OOS
+16.3 at 1h vs +36.1 at 15m on overlapping windows), so this does NOT
prove the 15m configuration loses on extended history — it proves the
SIGNAL at 1h does, and sharply cuts the prior that the 15m PASS is
durable; (b) b_edge2b (~Jun 20) remains the promotion gate, but its
verdict must now be read against this record — a 15m PASS whose profit
still lives entirely in the pocket is the warned-about shape. Both paper
agents stay (out-of-time forward test unaffected); nothing promoted,
nothing flipped. min_span_days bumped 150→200 post-run per protocol
(LIT binding → next rerun ~Jul 10, same day as b_edge3's).

**Evidence.** 481 → **482 tests pass**; ruff clean. Experiment live-run on
the real store (numbers above); verdict record committed beside the specs.

**Found.** The b_edge3/b_edge2_1h rerun pair now lands the same week
(~Jul 10) — one iteration can take both readouts.

**What's next (loop).** Per-iteration: the three `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — read against B-EDGE2g; b_g014 ~Jun 26; b_edge3 +
b_edge2_1h ~Jul 10). B-EDGE2f at ≥30d paper books (~Jul 8); xmom paper card
~Jul 12. Idle queue unchanged (B-SCALE doc on real G2 evidence; per-agent
funding clamp if mixed funding-sign agents ever share the live book). Edge
hunt status: twap_mr (proven, live) + breakout-ER (paper, now under a
regime-fragility cloud pending b_edge2b).

## Iteration 76 — 2026-06-12 — B-POCKET: profit time-concentration is now a number in every confirm

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m 52.4d
< 60d, ~Jun 20); b_g014 NOT RIPE (1m 4.0d < 14d, ~Jun 26); b_edge3 +
b_edge2_1h NOT RIPE (1h LIT 171.9d < 200d, ~Jul 10). Store healthy, zero
missing bars across all universes.

**Why this.** Iters 74–75 ended on the same warning: the Jun-20 b_edge2b
verdict must be read against the possibility that a 15m PASS's profit
"still lives entirely in the Apr–Jun pocket" — but the harness had no
number for that shape, so the most important read of the month was going
to be an eyeball call. Two strategy families died of exactly this in two
days. Making the pocket a first-class, automatically-recorded metric is
the highest-leverage unblocked move while all four specs wait on the
calendar.

**Changed.**
- `confirm.max_window_pnl_share(equity_curve, window_frac=0.25)` — pure,
  O(n) (sliding-window minimum over the per-bar equity curve): largest
  share of total net PnL earned in any contiguous window spanning 25% of
  the sample's calendar time, plus that window's timestamps. Reads:
  ~0.25 = diffuse edge, ~1.0 = one pocket, >1.0 = the rest of the sample
  LOSES. None for losing/too-short runs (concentration of a loss is not
  the diagnostic).
- `ScenarioResult` grows `pocket_share` / `pocket_window` (UTC dates) /
  `pocket_window_frac` (frac recorded per-field so a future constant
  change can't silently redefine old records). Computed uniformly in
  `_run` for IS, OOS, and every cost-ladder rung; shown in `row()` +
  a legend line in `summary()`; flows into `hlbot confirm`,
  `hlbot experiment`, and persisted verdict records via the existing
  `asdict` serialization — zero changes to experiments.py.
- **Verdict logic untouched** (confirmed-bit identical by construction;
  pinned by test) and **frozen specs unmodified** — informational only,
  the decision rules stay as pre-registered.

**Evidence.** 482 → **488 tests pass** (+6: diffuse≈0.25 / pocket≈1.0 with
correct dates / >1 when rest loses / None on losing+thin curves / confirm
wiring incl. frac pin / no pocket noise on losing summaries; record test
extended to pin persistence); ruff clean. Live-fired on the real 15m
store — the combined-ER breakout arm (b_edge2b's promotion-relevant
config, re-read on the ALREADY-SEEN 52.4d sample, so not a peek: the
spec's gate waits on post-Jun-12 data): still ✅ CONFIRMED (taker IS
+47.7 / OOS +35.6bps — window rolled a few hours vs Iter 49), and the
new diagnostic says the quiet part out loud: **taker-1x pocket 0.69
(May 25–Jun 5), in-sample 0.87 (May 19–26 — one week is 87% of IS
profit), OOS 2.20 (Jun 2–5 — outside one 4-day burst the OOS tail
loses money)**. The current G0 PASS is the pocket, quantified — the
Jun-20 rerun now has a baseline to beat instead of an adjective.

**What's next (loop).** Per-iteration: the four `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — read pocket_share against today's 0.69/0.87/
2.20 baseline; b_g014 ~Jun 26; b_edge3 + b_edge2_1h ~Jul 10). B-EDGE2f at
≥30d paper books (~Jul 8); xmom paper card ~Jul 12. Idle queue: B-SCALE
doc on real G2 evidence; per-agent funding clamp if mixed funding-sign
agents ever share the live book. Possible follow-up if Jun-20 needs it:
a pocket-aware reading aid in `hlbot experiment` output (the records
already carry the numbers).

## Iteration 77 — 2026-06-12 — B-FUNDGR2: per-agent funding clamp in the daily-loss guardrail

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m 52.4d
< 60d, ~Jun 20); b_g014 NOT RIPE (1m 4.0d < 14d, ~Jun 26); b_edge3 +
b_edge2_1h NOT RIPE (1h LIT 171.9d < 200d, ~Jul 10). Store healthy, zero
missing bars across all universes.

**Why this.** All four pre-registered specs wait on the calendar; the top
idle-queue item was the B-FUNDGR (Iter 68) noted-not-done follow-up:
`check_guardrails` clamped 24h funding income to zero on the AGGREGATE —
`min(0, Σ funding)` — so with mixed funding signs on the book one agent's
collection masks another's bleed: +$50 carry income vs −$8 femr bleed
counted $0 funding, and the bleed never tightened the daily-loss halt.
Today's live book is single-strategy so this is a live no-op, but B-SCALE's
multi-agent growth (a carry collector beside a funding-paying mean-reverter
is exactly the intended mix) would have inherited the hole — and risk rails
get built BEFORE the book needs them, like B-AGG (Iter 67).

**Changed.**
- `scoring/metrics.py`: new `agents_funding_breakdown(conn, agents,
  since_ms) -> dict[agent, signed USDC]` — same size-weighted attribution
  (deduped names, manual coins unattributed), kept per-agent;
  `agents_funding_since` reimplemented as its sum, so there is exactly one
  attribution path and the two can never diverge.
- `exec/orders.py` `check_guardrails`: daily-loss measure now adds
  `Σ_agent min(0, funding_agent)` instead of `min(0, Σ funding)` —
  strictly tighter (tightening-only by construction), byte-identical when
  every agent's funding shares a sign (all existing same-sign tests pass
  unchanged). Attribution failure still degrades to fills-only with a
  warning (never aborts ahead of risk-reducing flattens). Breach message
  shows both the signed total and the counted clamp
  (`funding $+42.00, counted $-8.00`) so an operator can reconstruct the
  verdict.

**Evidence.** 492 → **494 tests pass** (+2: guardrail halts on the masked
mixed-sign book — aggregate clamp would pass it; breakdown per-agent
values + dedup + sums-to-rollup pin); ruff clean. No live behavior change
(single funding-sign roster).

**What's next (loop).** Per-iteration: the four `--check-only` readouts
(b_edge2b ~Jun 20 FIRST — read pocket_share against the 0.69/0.87/2.20
baseline; b_g014 ~Jun 26; b_edge3 + b_edge2_1h ~Jul 10). B-EDGE2f at ≥30d
paper books (~Jul 8); xmom paper card ~Jul 12. Idle queue: B-SCALE doc on
real G2 evidence; possible pocket-aware reading aid in `hlbot experiment`
output if the Jun-20 read needs it.

## Iteration 78 — 2026-06-12 — store-continuity guard was dead in the running loop; fixed via PROMPT step 0 + `--if-stale-minutes`

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m 52.5d
< 60d, ~Jun 20); b_g014 NOT RIPE (1m 4.1d < 14d, ~Jun 26); b_edge3 +
b_edge2_1h NOT RIPE (1h LIT 172.0d < 200d, ~Jul 10). Store topped up this
iteration (all 60 pairs ok), zero missing bars.

**Why this.** All four pre-registered specs wait on the calendar — and the
thing they wait on is an UNBROKEN candle store. Iters 71/72 both flagged the
same mystery ("/tmp/ralph_harvest.log absent, yet the store is fresh; we
can't tell which mechanism is keeping it alive") and took no action.
Diagnosed it this iteration: **loop.sh's per-iteration top-up (Iter 33) has
NEVER executed in the running loop.** The loop process (PID 4224,
hlbot-loop.service) started 00:03 Jun 12; the harvest step landed in
loop.sh at 02:08 (commit 36742e7). Bash parses the whole `for` body at
startup, so the running loop's in-memory body predates the step — editing
loop.sh can never reach an already-running loop. ralph_pytest.log/
ralph_ruff.log (also written per-iteration, by code present at startup) ARE
in /tmp; ralph_harvest.log isn't. The store survived on agents' INCIDENTAL
in-session harvests (latest: Iter 74's 1h harvest at 12:34) — nothing
guaranteed one per iteration. 1m retention is ~3.5d; a weekend of
iterations that happen not to harvest would permanently gap B-G014's 14d
sample and push its ETA out by weeks — while BACKLOG explicitly claimed
"store continuity now guarded by loop.sh per-iteration top-up" (false;
claim now corrected).

**Changed.**
- `backtest/store.py`: `harvest_pairs` (the swept grid, extras deduped —
  now shared by `harvest` and the CLI) + `worst_store_lag(pairs)` → the
  most-lagging pair's `(label, lag_minutes)`, measured in DATA terms (time
  beyond one full interval since the pair's last stored bar opened — a
  just-harvested store reads ~0 at every interval, so one threshold works
  across 1m and 1h pairs; no stored bars / empty pair list → None =
  maximally stale, fail toward harvesting). File mtimes deliberately not
  used: data-truth, pure, offline-testable.
- `hlbot harvest-candles --if-stale-minutes N`: skips the network entirely
  when worst lag ≤ N (default 0 = always harvest, byte-identical to
  before). Lets overlapping backstops all run unconditionally without
  double-fetching.
- `ralph/PROMPT.md` **step 0**: every iteration runs `uv run hlbot
  harvest-candles --if-stale-minutes 30` (no-op when fresh; network failure
  must not block the iteration). The prompt is the ONE per-iteration input
  the running loop re-reads from disk (`$(cat "$PROMPT_FILE")` executes
  each pass), so this reaches the live process — unlike loop.sh edits.
- `ralph/loop.sh`: harvest line gains `--if-stale-minutes 30` + a comment
  documenting the parse-at-startup trap; activates on the next
  `hlbot-loop.service` restart (operator's call, no urgency — PROMPT step 0
  is the active guard either way).

**Evidence.** 494 → **500 tests pass** (+6: lag≈0 across intervals right
after harvest / worst-pair selection with per-interval step / missing store
+ empty pairs → None / harvest_pairs dedup / CLI skip-when-fresh wiring
[harvest must not run] / CLI runs-when-stale-or-missing); ruff clean.
Live-fired: real harvest this iteration (60/60 pairs ok), then
`--if-stale-minutes 30` → "store fresh (worst lag 2.5m at ADA_1m ≤ 30m) —
skipping harvest", <1s, zero API calls.

**Found.** (a) The Iter-71/72 harvest-log mystery is fully explained (no
PrivateTmp involved; the code simply never ran). (b) Meta-lesson for any
future loop.sh change: it only takes effect on service restart — anything
that must reach the RUNNING loop goes in PROMPT.md.

**What's next (loop).** Per-iteration: PROMPT step 0 (store top-up), then
the four `--check-only` readouts (b_edge2b ~Jun 20 FIRST — read
pocket_share against the 0.69/0.87/2.20 baseline; b_g014 ~Jun 26; b_edge3
+ b_edge2_1h ~Jul 10). B-EDGE2f at ≥30d paper books (~Jul 8); xmom paper
card ~Jul 12. Idle queue: B-SCALE doc on real G2 evidence; pocket-aware
reading aid in `hlbot experiment` output if the Jun-20 read needs it.
Operator note (non-urgent): restart `hlbot-loop.service` at convenience to
activate loop.sh's own stale-gated top-up.

## Iteration 79 — 2026-06-12 — B-DEPLOY-EXEC: auto-deploy was dead on every box (update.sh never had the exec bit); live box frozen 55 commits back

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m 52.5d
< 60d, ~Jun 20); b_g014 NOT RIPE (1m 4.1d < 14d, ~Jun 26); b_edge3 +
b_edge2_1h NOT RIPE (1h LIT 172.0d < 200d, ~Jul 10). Store fresh (step-0
check: worst lag 4.9m ≤ 30m, no harvest needed), zero missing bars.

**Why this.** Routine box check while all four specs wait on the calendar:
`hlbot-update.service` was in `failed` state — `status=203/EXEC`, every 15
minutes, on the unit whose whole job is pulling the loop's improvements
into the live deployment. Root cause: `deploy/update.sh` was git-tracked
**100644 from birth** (added Jun 8, 365c700), so EVERY checkout produces a
non-executable script and systemd's bare `ExecStart=` can never run it —
auto-deploy has never executed once. Compounding trap: the operator had
already fought this exact symptom (8dad672 "git config core.fileMode false
so chmod's exec-bit changes don't block pull/auto-deploy") — but
fileMode=false makes a workdir `chmod +x` INVISIBLE to `git add -A`, which
is precisely why the +x never landed in git no matter how many times it
was applied locally. Both clones have fileMode=false. Blast radius: the
live box (this box — tick timer every 5m, HLBOT_AUTO_UPDATE=1, deploy
clone deliberately tracking the loop's branch) was frozen at its
install-day commit 8edda64, **55 commits behind** — running without
B-FUNDGR/B-FUNDGR2 (funding in the daily-loss halt), B-GR1 (snapshot-
consistent guardrails), B-M5 (spot-mid fix), B-HB (tick heartbeat), B-AGG
(aggregate 5× cap)… every risk rail of the last 3 days was protecting
nothing. Evidence this is a bug, not an operator freeze: the env gate is
ON, the timer is enabled and firing, and the failed unit was retrying
every 15 minutes.

**Changed.**
- `deploy/update.sh` index mode → 100755 via `git update-index --chmod=+x`
  (the only way that sticks under fileMode=false; content untouched).
- `deploy/systemd/hlbot-update.service`: `ExecStart=/usr/bin/bash
  /opt/hl-bot/deploy/update.sh` + comment — a future lost exec bit can
  never re-kill auto-deploy. Reaches the box via update.sh's own unit-cp
  step on the first successful run (no manual unit copy needed).
- `tests/test_deploy_exec_bits.py`: pin — every tracked `deploy/**/*.sh` +
  `ralph/loop.sh` (all direct ExecStart/operator entry points) must be
  index-mode 100755; checks the GIT INDEX (workdir bit is a lie under
  fileMode=false), degrades to os.access when git is unavailable. A future
  Write-tool rewrite of any entry-point script that drops the bit fails CI.
- Box remediation (deploy clone, `/opt/hl-bot`): `chmod +x
  deploy/update.sh` — invisible to the ff-only merge under fileMode=false,
  activates the existing unit at the next timer fire.

**Evidence.** 500 → **501 tests pass**; ruff clean. Live-fired: watched the
15:19:26 UTC timer fire — first successful auto-deploy in the unit's
history: ff-merged 8edda64 → 4d3454b (all 55 commits), box-side test gate
green (500 passed in 10.08s), `.deployed_sha=4d3454b`, unit
`Result=success`, hlbot-ws + hlbot-tick.timer restarted and active (next
tick 15:21:56). The live book now runs this week's risk rails.

**Found.** (a) Same class as Iter 78's loop.sh trap, one layer down: Iter 78
fixed "edits don't reach the running loop"; this fixes "the updater that
ships edits to the live bot never ran at all". The two failures were
masking each other — a working updater would have surfaced loop.sh's
staleness sooner. (b) fileMode=false is a standing trap for ANY future
script added to deploy/: it will ship 644 unless `git update-index
--chmod=+x` is used explicitly; the new test turns that mistake into a CI
failure instead of a silent dead unit. (c) Operator files `_arm.py` /
`_setlive.py` sit untracked in /opt/hl-bot — left untouched.

**What's next (loop).** Per-iteration: PROMPT step 0 (store top-up), then
the four `--check-only` readouts (b_edge2b ~Jun 20 FIRST — read
pocket_share against the 0.69/0.87/2.20 baseline; b_g014 ~Jun 26; b_edge3
+ b_edge2_1h ~Jul 10). Next iterations should confirm hlbot-update keeps
deploying (this iteration's own commit is the natural test case — it
should land on the box within ~15m of push). B-EDGE2f at ≥30d paper books
(~Jul 8); xmom paper card ~Jul 12. Idle queue: B-SCALE doc on real G2
evidence; pocket-aware reading aid in `hlbot experiment` output if the
Jun-20 read needs it.

## Iteration 80 — 2026-06-12 — B-POCKET2: pocket-aware prior-run comparison in `hlbot experiment` + b_edge2b baseline recorded

**Ripeness checks** (per-iteration readout): b_edge2b NOT RIPE (15m 52.5d
< 60d, ~Jun 20); b_g014 NOT RIPE (1m 4.1d < 14d, ~Jun 26); b_edge3 +
b_edge2_1h NOT RIPE (1h 172.0d < 200d, ~Jul 10). Store fresh (step-0:
worst lag 19.7m ≤ 30m, no harvest). Auto-deploy confirmed alive post-
B-DEPLOY-EXEC: 15:19 timer fire Result=success, box at 4d3454b (d12dac2
lands next cycle after push — the Iter-79 "natural test case").

**Why this.** All four specs are calendar-blocked, so this is the idle-queue
item: the Jun-20 b_edge2b read is the first gate decision, and its protocol
("read pocket_share against the 0.69/0.87/2.20 baseline; a PASS whose
pocket numbers don't fall on new data is the pocket renewing its badge")
existed only as prose in BACKLOG/PROGRESS — the headline experiment table
had no pocket column, and prior verdicts sat in raw JSON. A reading the
machinery doesn't surface is a reading a future iteration can skip.

**Changed.**
- `backtest/experiments.py`: `load_experiment_records(spec_name, dir)`
  (all persisted verdicts for a spec, ran_at-sorted, peeks included,
  garbage/foreign files skipped — a reading aid, never a gate),
  `preferred_full_scenario` (the cost-ladder rung matching an arm's
  execution basis: taker→taker-1x, maker→the maker rung whatever its fill
  named it), `arm_comparison` (compact row: verdict, IS/OOS edge+trades,
  pocket triple; pre-B-POCKET records degrade to None, never fail).
- `cli experiment`: current-run table grows `pocket is/oos/1x`; every full
  run prints a "Prior recorded runs" table (loaded BEFORE the new record is
  written) with per-arm verdict/edge/pocket per prior record, peeks
  labeled. `--check-only` unchanged (per-iteration readout stays terse).
- Recorded the b_edge2b baseline honestly: ran the frozen spec `--force` on
  the already-seen 52.5d sample → `results/b_edge2b.20260612T153311Z.peek.
  json` (visibly a peek, forced:true — same numbers Iter 76 published as
  the Jun-20 baseline, now machine-readable where the comparison reads).

**Evidence.** 501 → **504 tests pass** (+3: record loading filters/sorts/
skips garbage + missing dir; arm_comparison rung selection (taker-1x vs
maker-rest) + legacy degrade; CLI pocket column + prior-table appears only
once a record exists, pocket triple shown in both current and prior rows);
ruff clean. Live-fired: the forced peek printed the new pocket column on
real data, and `load_experiment_records`+`arm_comparison` read the real
record back correctly.

**Found.** Today's window (one day later than Iter 76) already moves the
pockets: combined-ER OOS pocket 2.20→1.81, IS 0.87→0.86, taker-1x 0.69
(stable). More interesting: the **original-universe arm's full-sample
pocket is 1.15** — outside its best 13-day window the arm LOSES on the
full sample; its PASS (+19.8 IS / +61.6 OOS) is wholly the May-25–Jun-5
pocket. breadth arm OOS pocket prints "—" by design (OOS lost money;
concentration-of-a-loss isn't computed). The Jun-20 comparison is now
mechanical: combined-ER must keep pocket ≲0.7 at taker-1x with the OOS
pocket FALLING as post-selection data accrues, or the PASS is the pocket.

**Found (deploy regression, remediated in-iteration).** The 15:34:35
hlbot-update run failed **203/EXEC again** — the 15:19 deploy Iter 79
watched succeed had itself re-broken the updater: the ff-merge to 4d3454b
(one commit BEFORE the d12dac2 fix, which wasn't pushed until Iter 79's
loop pass ended) rewrote `deploy/update.sh` (content changed in-range at
1556f7b) from the git index at mode 100644, stripping the manual
`chmod +x`. So the fix that makes the updater immune sat on GitHub,
unfetchable by the dead updater it fixes — a chicken-and-egg the Iter-79
entry didn't see: a deploy-mechanism fix can't ride the mechanism while
the broken state re-manifests in the remediation→push gap. Re-applied the
documented remediation (`chmod +x`, as hlbot, file owner); the next fire
(15:49:35) fetches d12dac2, whose checkout sets index mode 100755 (the
bit now survives merges) and whose unit-cp installs the bash-prefixed
ExecStart — both layers of the permanent fix. Deploy verification noted
below. Follow-up filed (B-DEPLOY-HB): the updater was dead Jun 8–12 and
failed→dead again today with nothing paging — `hlbot health` has no eyes
on hlbot-update.

**What's next (loop).** Per-iteration: PROMPT step 0 (store top-up), the
four `--check-only` readouts (b_edge2b ~Jun 20 FIRST; b_g014 ~Jun 26;
b_edge3 + b_edge2_1h ~Jul 10), and spot-check the box keeps auto-deploying
(d12dac2+this commit should land within ~15m of push). B-DEPLOY-HB
(updater visibility in `hlbot health`) is the next idle-queue candidate.
B-EDGE2f at ≥30d paper books (~Jul 8); xmom paper card ~Jul 12. Also
queued: B-SCALE doc on real G2 evidence; consider a reversal read of
xmom's strongly-negative IS on the 208d 1h sample (sign-flip hypothesis —
needs cost math first; do NOT burn the pre-registered b_edge3 rerun on
it).

## Iteration 80 (part 2) — 2026-06-12 — LIVE INCIDENT: demote-with-open-inventory orphaned a $390 book on a $49 account; fixed with exit-only live management

**Discovery chain.** Closing out the deploy watch found `tick_heartbeats`
EMPTY despite 4 post-deploy tick fires (run-tick.sh masks step failures —
unit shows SUCCESS regardless). A manual PAPER tick on the box completed
fine (heartbeat written), so the break was live-only. agent_state showed
twap_mr_v1 `mode=paper, notes='demoted by supervisor'`; goal_evaluations
told the whole story: **15:07 the OLD pre-deploy code auto-promoted
twap_mr_v1 paper→live_small off paper cards** (the path B-PAPER3c closed —
but that fix sat undeployed for 4 days behind the dead updater, B-DEPLOY-
EXEC), **15:12 it entered NEAR (filled) and rested TON** at ~$195 notional
each (the old auto-tuner's $200/trade standing approval — B-AGG's 5× cap
was also undeployed), **and the edge guardrail demoted it the same tick**
(7d realized edge −10.4bps < −10). TON's rest filled later; with the agent
demoted, `filter_live_agents` dropped it and femr_tick's empty-roster
early return skipped EVERYTHING: no exits, no maker-fill reconcile (the
DB never learned it owns TON), no stale-quote cancels, no guardrail pass,
no heartbeat. Exchange truth at discovery: TON short $195 + NEAR short
$195 = **$390 notional, ~8× leverage on $49.24**, +$9.6 uPnL, zero open
orders, zero management, and both pager channels empty. The B-DEPLOY-EXEC
incident and this one compound: every safety rail of the last 3 days
existed in git while the live book ran a 4-day-old promotion footgun.

**Changed** (second commit this iteration).
- `runtime.exit_only_live_agents(conn, agents, live_names)`: skipped-from-
  live agents that still have live-book ownership (`bot_owned_coins`
  live-only) or working maker quotes re-enter the live tick EXIT-ONLY.
  Paper-book state never qualifies (B-PAPER separation holds).
- `runtime.execute_decisions(exit_only=)`: any `place` from an exit-only
  agent is dropped BEFORE guardrail/cooldown checks; `flatten` executes
  always (incl. under guardrail halt — risk reduction, unchanged).
- `cli.femr_tick`: live mode appends exit-only managers to the tick roster
  (prints them), so their exits, maker reconcile + stale cancels, and the
  guardrail check all run; the empty-roster early return now records a
  live heartbeat (alive-but-refusing ≠ dead).
- NOT a promotion path: entries stay gated on agent_state exactly as
  before; an exit-only agent's exposure can only shrink.

**Evidence.** 504 → **506 tests pass** (+2: exit-only selection — holder
via live book / rester via working quote / paper-only and flat books and
live-roster agents excluded; execution gate — entry skipped with green
guardrails + no exchange call + nothing logged, flatten executes under a
halt); ruff clean. Verified read-only against the REAL box DB:
`working_orders` shows the unreconciled TON rest (112.58 @ 1.7773 — the
exchange position exactly) and `exit_only_live_agents` selects
twap_mr_v1. Deploy verified end-to-end this iteration: the 15:49:35 cycle
shipped d12dac2 (exec bit now from the git index, hardened
`ExecStart=/usr/bin/bash` unit installed at /etc/systemd/system).

**Expected post-deploy behavior** (operator note): within ~2 ticks of this
commit landing, twap_mr_v1 re-enters the tick exit-only, the TON fill
reconciles to ownership, and both positions exit via its ladder — both are
far past the 4h max-hold, so expect two market closes (currently in
profit, +$9.6 at discovery). The notional guardrail will print HALT (390 >
5×49) — entries are blocked anyway; flattens proceed. If you would rather
close manually first, do it before this commit deploys (~15m after push).

**Found / filed.** (a) B-DEPLOY-HB (above): two compounding multi-day
silent failures this week — updater dead, then live loop dead-by-refusal —
and `hlbot health` saw neither; deploy staleness needs a check, and the
empty pager env (HEALTHCHECK_URL/TG_CHAT_ID) deserves an operator nudge.
(b) Policy question for the operator (in B-EXITONLY): flatten-on-demote
would be stricter than exit-ladder unwind. (c) run-tick.sh's best-effort
steps mask femr_tick exit codes — fine by design now that the heartbeat
distinguishes alive/dead, but worth remembering when reading unit status.

**What's next (loop).** Watch the next iterations: (1) this commit deploys
and the orphaned book closes — verify in fills/positions and that
heartbeats resume on live ticks; (2) per-iteration readouts unchanged
(b_edge2b ~Jun 20, b_g014 ~Jun 26, b_edge3+b_edge2_1h ~Jul 10). Then
B-DEPLOY-HB. Idle queue: B-SCALE doc on real G2 evidence; xmom-reversal
cost math.
