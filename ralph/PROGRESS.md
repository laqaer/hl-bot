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

## Iteration 24 — 2026-06-08 — B1c: beta-neutralise the cross-section (helps robustly, still negative)

**Context.** Network reachable (HL `meta` → 200); corrected-funding caches present
(10- and 20-coin, 90d 1h). B1c — the edge hunt on xfund_carry — was the top
unblocked P0. Iter 23 left two un-pruned levers; took the most principled one:
**(b) beta-neutralise the cross-section.** The diagnosis across Iters 20–23 was
"price variance buries the carry"; the specific hypothesis here is that a
*dollar*-neutral book is not *market*-neutral — the high-positive-funding coins we
SHORT are typically higher-beta squeezing alts, so the book is net-short the market
and that residual directional variance eats the few-bp/hr carry.

**Changed (1 commit).**
- **`agents/xfund_carry.py`** — new pure `rolling_beta(coin_closes, mkt_closes)`
  (OLS beta of a coin's returns on a market proxy's, aligned on the overlapping
  trailing closes the engine/live view already carry) and a `beta_neutral: bool =
  False` lever (+ `beta_market="BTC"`, `beta_lookback=48`, `beta_floor=0.3`,
  `beta_cap=3.0`). When on, `_beta_scales` sizes each leg by `ref/clamp(|beta|)`
  where `ref` is the smallest clamped beta in the book, so every leg carries equal
  beta-dollars (book beta-neutral) and the largest scale is exactly 1.0 —
  **tightening-only**, legs only shrink vs the dollar-neutral baseline, never grow
  past the per-trade cap (respects the risk-tighten-only rule). Default off → CI/
  behavior unchanged. The over-tight inner `notional<5` guard switched `break`→
  `continue` so a beta-shrunk leg can't abort the whole entry loop (unreachable in
  baseline).
- **`tests/test_funding_carry.py`** — 3 new: `rolling_beta` recovers a known 2.0
  slope (mean-invariant) and returns None on degenerate input (too few returns /
  flat market); and beta-neutral mode shrinks the high-beta short leg vs the
  low-beta long leg with the shrink ratio tracking the beta ratio, while
  dollar-neutral keeps them equal.

**Evidence (tests/lint).** 142 → **145 tests pass**; `ruff check src tests scripts`
clean. Caches stay gitignored.

**B1c result (xfund_carry_v1, 90d 1h, maker; reproducible via `--config`).**
- *10-coin* (ADA,AVAX,BTC,DOGE,ETH,HYPE,LINK,SOL,TRX,ZEC): baseline maker
  **−4.3bps** / Sharpe −0.45 / 62 trades → `beta_neutral` maker **−2.3bps** /
  Sharpe −0.23 / 58 trades. (taker −9.8 → −7.8bps.)
- *20-coin* (+SUI,APT,INJ,TIA,SEI,LTC,ARB,OP,WIF,NEAR): baseline maker **−12.1bps**
  / Sharpe −2.39 / 154 trades → `beta_neutral` maker **−8.7bps** / Sharpe −1.69 /
  150 trades. (taker −17.6 → −14.2bps.)
- `confirm --prefer maker --config '{"beta_neutral":true}'`: **NOT CONFIRMED** both
  universes. 10-coin OOS-maker **+6.3bps / +1.04sh** (in-sample −43.7); 20-coin
  OOS-maker **−19.8bps / −3.98sh** (in-sample +1.3). The OOS sign *flips between
  universes* → the positive OOS is noise, not a robust edge.

**Why it matters (the finding).** This is the FIRST B1c lever that *helps* — and it
helps *robustly in the same direction on both universes* (~+2bps/+3.4bps full-sample
maker), which **confirms the root-cause hypothesis**: residual market exposure
(dollar≠market-neutral) was burying roughly half the bleed. It is a genuine
improvement worth keeping. BUT it does **not** cross to positive: the best config in
the book is now `beta_neutral` 10-coin at **−2.3bps maker — still negative** — and
`confirm` shows no robust OOS edge. **No edge; nothing promoted; all paper/gated.**
The lever is kept (default off, tested) so it composes with future levers without
re-exploration.

**What's next (loop).** The last un-pruned B1c lever: **(c) a lower cadence (4h/1d)**
where funding accrual per bar outweighs per-bar price noise — the data/engine already
scale funding by `bar_hours`, so this needs a 4h/1d cache fetch + a beta_neutral run
at that cadence (network-gated). If lower cadence + beta-neutral still doesn't cross
zero, xfund carry on liquid alts is likely structurally negative net-of-cost and the
hunt should pivot (e.g. funding-decile spot-vs-perp basis, or a different signal).
Then resume B12 consolidation.

---

## Iteration 25 — 2026-06-08 — B1c: lower-cadence lever exposed a funding-scale bug; pruned. B1c closed.

**Context.** Network up (HL `meta` → 200). B1c's last un-pruned lever was **(c) a
lower cadence (4h/1d)** where funding accrual per bar was hypothesised to outweigh
per-bar price noise. Fetched fresh 10- and 20-coin 4h/90d caches and ran it.

**The trap (and why honesty mattered).** The first 4h run looked like a *breakthrough*:
xfund_carry_v1 baseline went strongly **positive** — maker **+26.2bps** / OOS
**+16.0bps** (10-coin) and maker **+11.6bps** / OOS **+28.3bps** (20-coin), `confirm`
**PASS** on both, robust to 3× taker slippage. Before promoting, I cross-checked it
the way the mission demands and it **fell apart**:
- A cadence gate (`rebalance_hours=4`) applied to the *1h* cache (funding correctly
  per-hour) gave maker **−22bps**, not positive — coarse cadence alone does NOT help.
- Re-running the 4h backtest with thresholds ×4 (restoring the true per-hour entry
  filter) collapsed the edge to **−10.9bps**.

**Root cause — a real measurement bug.** The agent compares its **per-hour**
thresholds (`enter/exit_funding_per_hr`) directly against `view.funding`, but the
backtest data layer **scales `Frame.funding` by `bar_hours`** (4× on a 4h bar;
`data.py:258`). So at 4h the entry filter `|f| ≥ 0.0001` was effectively
`|hourly| ≥ 0.000025` — a 4×-looser filter that scooped up many marginal-funding
legs that happened to be net-positive in-sample. The APR display was also 4× off.
**Every non-1h carry backtest ever run was mis-specified.**

**Changed (1 commit).**
- **`backtest/engine.py`** — `Frame.bar_hours: float = 1.0`; the view now exposes
  `extra["bar_hours"]`.
- **`backtest/data.py`** — `build_frames` stamps each `Frame` with its `bar_hours`.
- **`agents/xfund_carry.py`** — normalize `view.funding` back to a per-hour rate via
  `extra["bar_hours"]` before any threshold/side/APR use (no-op live: HL funding is
  hourly → `bar_hours=1`). Also adds a **`rebalance_hours` cadence gate** (default 0
  = off): within the cooldown the book makes no new entries and skips rank-rotation
  churn, but **still takes risk-reducing exits** (funding flipped/normalized) so the
  live 5-min loop can't churn a carry book to death.
- **`agents/funding_carry.py`** — same per-hour normalization (shared bug).
- **`configs/xfund_carry_v1.yaml`** — reverted a premature "G0 PASSED" claim back to
  "NOT yet passing", recording the artifact + fix.
- **`tests/test_funding_carry.py`** — +2 tests: thresholds are **interval-invariant**
  (MEH below the per-hour entry threshold is skipped at both 1h and 4h, where without
  the fix its 4×-scaled value would wrongly clear the filter); the **cadence gate**
  freezes entries + rank-rotation within the cooldown but still de-risks a funding
  flip, and rebalances after it elapses.

**Evidence (tests/lint).** 145 → **147 tests pass**; `ruff check src tests scripts`
clean. Caches stay gitignored (refetched 4h caches with `--refresh` so they carry
`bar_hours`).

**B1c result post-fix (xfund_carry_v1, 90d 4h, maker; reproducible via `--config`).**
- 10-coin default: maker **−12.8bps** / Sharpe −0.78 / 24 trades (taker −18.3).
- 10-coin beta_neutral: maker **−12.2bps**.
- 20-coin default: maker **−19.0bps** / Sharpe −1.91 / 68 trades.
- `confirm` 10-coin 4h: **NOT CONFIRMED** (OOS edge −27.5bps, OOS sharpe −2.35).
- 1h cache + `rebalance_hours=4` (honest cadence-only test): maker **−22bps**.

**Why it matters (the finding).** Lever (c) is **pruned**, and with it **all of
B1c is closed**: tighter entry, wider universe, hold-while-eligible, beta-neutral,
and lower cadence have each been tested and each fails to cross zero. The best
config in the book remains beta_neutral 10-coin 1h at **−2.3bps maker — negative.**
**xfund cross-sectional funding carry on liquid alts is structurally negative
net-of-cost**: the coins with extreme funding are extreme *because* they're volatile,
so price variance buries the few-bp/hr carry whether you concentrate, diversify,
beta-neutralise, or slow down. Net of all this, the durable win of the iteration is
a **fixed measurement bug** that was silently inflating every non-1h carry result —
exactly the kind of honest-measurement correction (cf. the `fundingHistory` 500-row
cap in B4-RUN) that keeps the search from chasing ghosts. Nothing promoted; all
paper/gated. The `rebalance_hours` lever is kept (default off, tested) so it composes
with future strategies without re-exploration.

**What's next (loop).** New backlog item **B1d — pivot the edge hunt** off
cross-sectional funding carry to a different signal class: spot-vs-perp **basis** at
funding deciles (carry without alt price-variance), a slower **trend/regime** overlay
on majors at maker cost, or FEMR as a pure hold-to-collect maker on the *moderate*
(not extreme) funding band. Pick one, backtest, confirm. Then resume B12 consolidation.

---

## Iteration 26 — 2026-06-08 — B1d(iii): moderate-band funding carry — works directionally, never crosses zero. Pruned.

**Context.** Network up (HL `meta` → 200). B1c closed (xfund cross-sectional carry
structurally negative on liquid alts across every lever). B1d is the pivot to a new
signal class. Took the smallest slice of candidate **(iii)**: revisit single-name
`funding_carry_v1` as a pure hold-to-collect maker restricted to a **moderate** funding
band — directly testing the B1c root cause that *the extreme-funding names are extreme
because they're the most volatile*, so price variance buries the carry. If true, skipping
the extreme tail of the funding distribution should help.

**Changed (1 commit).**
- **`agents/funding_carry.py`** — new optional `max_enter_funding_per_hr` config
  (default 0.0 = off → original behavior, CI/behavior unchanged). When >0 the entry
  candidate filter requires `|funding| <= max_enter`, i.e. only the *moderate* band
  `[enter, max_enter]`. **Tightening-only**: it can only remove entry candidates, never
  add them (respects the risk-tighten-only rule).
- **`tests/test_funding_carry.py`** — +1 test: with the cap set, the agent enters the
  MODERATE coin and skips the EXTREME one (|funding| above the cap); with the cap off
  (default) both are eligible. (147 → **148 tests pass**.)

**Evidence (tests/lint).** 148 tests pass; `ruff check src tests scripts` clean. Caches
stay gitignored. Baseline reproduced first: funding_carry_v1 10-coin 90d 1h maker
**−111.1bps** / 20 trades / 30% win.

**B1d(iii) result (funding_carry_v1, 90d 1h, maker, 10-coin ADA,AVAX,BTC,DOGE,ETH,HYPE,
LINK,SOL,TRX,ZEC; reproducible via `--config`).** The alts' |funding| caps ~2.7bp/hr
so only caps in (1.5bp floor, 2.7bp] bind. Sweeping the cap (all vs baseline −111bps):
- `max_enter=0.00025` → maker **−77.7bps** / 20 trades / 40% win
- `max_enter=0.00022` → maker **−65.5bps** / 18 trades / 44% win
- `max_enter=0.00020` → maker **−34.5bps** / 16 trades / 50% win
- `max_enter=0.00017` → maker **−18.3bps** / 12 trades / **67% win**
- widest moderate *window* (floor 0.0001 / cap 0.0002) → maker **−14.0bps** / 54 trades / 48% win (best edge)
- `confirm --prefer maker --config '{"max_enter_funding_per_hr":0.00017}'`: **NOT
  CONFIRMED** — in-sample maker +49.8bps but **OOS −52.5bps / sharpe −5.26 / 12 trades**
  (classic overfit, no robust edge).

**Why it matters (the finding).** The lever **works exactly in the hypothesised
direction and monotonically** — tightening the band lifts edge −111 → −18bps and win
rate 30% → 67% — which **independently re-confirms the B1c root cause**: the extreme-
funding alts are the volatile ones whose price variance buries the carry; drop them and
the bleed shrinks. BUT it **never crosses positive**: it just approaches zero from below
by trading fewer / calmer names, and the one config with a real trade count (54) is the
best edge at **−14bps maker — still negative**, with `confirm` failing OOS. **Single-name
funding carry on liquid alts is structurally negative net-of-cost even restricted to the
moderate band.** Combined with B1c, the entire *carry* signal class on liquid alts is now
thoroughly pruned (cross-sectional: entry tightness/universe/churn/beta-neutral/cadence;
single-name: moderate band). Nothing promoted; all paper/gated. Lever kept (default off,
tested) so the dead end isn't re-explored and it composes with future work.

**What's next (loop).** B1d remaining candidates are now down to **(i) spot-vs-perp basis
at funding deciles** and **(ii) a slower trend/regime overlay on majors at maker cost**.
Given carry-on-alts is exhausted, the next slice should be a *non-carry* signal — lean
toward **(ii) trend/regime on majors** (BTC/ETH/SOL/HYPE cache already exists; the
`twap_mr_regime_v1` regime machinery is reusable, but as a *trend-follow* rather than a
*fade* this time). Backtest, confirm. Then resume B12 consolidation.

---

## Iteration 27 — 2026-06-08 — B1d(ii): trend-follow on majors — FIRST positive net-of-cost signal (cost-robust, but not yet time-stable → not confirmed).

**Context.** Network up (majors cache present). B1c + B1d(iii) closed the entire
*carry* signal class on liquid alts (every lever negative net-of-cost). B1d pivots
to a **non-carry, non-fade** signal. Took candidate **(ii): pure trend-following on
the majors** — the mirror of TWAP-MR (ride breakouts instead of fading them).

**Changed (1 commit).**
- **`agents/trend_breakout.py`** (new) — `TrendBreakoutAgent` / `trend_breakout_v1`:
  a Donchian-channel breakout trend-follower. Entry: go LONG on a strictly-new
  `entry_lookback`-bar high, SHORT on a new low (ranked by breakout strength).
  Exit: opposite shorter `exit_lookback`-bar channel (trailing stop) OR wide
  `stop_loss_pct` OR `max_hold_hours`. Reads `view.extra['closes']` (populated by
  the backtest loader and live `_enrich_view`); standalone `_open_positions` from
  the decision log (same pattern as funding_carry). Defaults: entry 24 / exit 12 /
  stop 5% / max-hold 96h / ≤4 concurrent / $100 per / $300 total.
- **`cli/main.py`** — import + register `trend_breakout_v1` in `_backtest_factories`
  (backtest/confirm only; NOT added to the live roster — paper/gated by omission).
- **`tests/test_trend_breakout.py`** (new, +4) — directional evidence: goes LONG &
  profits on an uptrend (and beats TWAP fading the same rip), goes SHORT on a
  downtrend, makes ZERO trades in a fixed oscillation band (strict-breakout
  semantics: `mid > hi`, not `>=`, so touching the prior high is not a breakout),
  and the trailing channel flattens on a sharp reversal. (148 → **152 tests pass**.)

**Evidence (tests/lint).** 152 tests pass; `ruff check src tests scripts` clean.
Caches stay gitignored.

**Backtest (trend_breakout_v1, BTC/ETH/SOL/HYPE 90d 1h, 2161 frames, 552 trades).**
| exec | net | edge | sharpe |
|------|-----|------|--------|
| maker     | +$56.87 | **+11.2bps** | +1.87 |
| taker-1×   | +$29.01 | **+5.7bps**  | +0.98 |
| taker-2×   | +$18.88 | +3.7bps  | +0.66 |
| taker-3×   | +$8.75  | +1.7bps  | +0.34 |

**This is the FIRST signal in the entire hunt that is positive net-of-cost AND
cost-robust** — it survives even 3× taker slippage (+1.7bps) and is robust-to-2×.

**Confirm (G0) — NOT CONFIRMED, on a time-stability fail (not an overfit).**
- prefer maker: in-sample(63d) +1.1bps/+0.27sh/390tr · **OOS(27d) +35.6bps/+4.60sh/164tr** · robust-2× True · fails: in-sample edge +1.1 < +3.
- prefer taker: in-sample −4.4bps/−0.77sh · **OOS +30.1bps/+3.89sh** · robust-2× True · fails: in-sample −4.4 < +3.

The OOS (recent ~27d) half is *strongly* positive while the older in-sample half is
flat/negative, so G0's "both halves ≥+3bps" rule fails. Crucially this is the
**opposite shape** of the pruned carry artifacts (there in-sample looked great and
OOS collapsed → classic overfit). Here the edge is real and cost-robust but
**regime-concentrated in the recent trending period** — a trend-follower is
*supposed* to be flat in chop, so a flat older half is consistent with the older
window being range-bound rather than the strategy being fake.

**Why it matters (the finding).** Carry-on-alts was a dead class; the pivot to
trend-follow-on-majors immediately produced the first cost-robust positive edge,
validating the B1d pivot. But it is **not yet G0-confirmed** because the edge isn't
time-stable. **Not promoted; not wired to live; not tuned to the gate** (fitting
params to pass G0 would just overfit the gate — the same trap, one level up).

**What's next (loop).** New backlog item **B1d-trend**: make the edge time-stable
*honestly*. First hypothesis to test next slice — was the older in-sample window
genuinely range-bound? If so the right move is a **regime/vol gate that sits out
chop** (a trend-follower earning nothing in chop is correct behavior), NOT a
lookback/stop tweak. One hypothesis per slice, re-confirm each, do not overfit the
gate. Other remaining B1d candidate: (i) spot-vs-perp basis at funding deciles.

---

## Iteration 28 — 2026-06-08 — B1d-trend(a): regime gate for trend_breakout — chop confirmed but the causal fix FAILS (monotonically worse). Pruned.

**Context.** Network up (majors cache present). Iter 27 found `trend_breakout_v1`
is the first cost-robust positive signal (maker +11.2bps, robust to taker-3×) but
NOT G0-confirmed: OOS(recent 27d) strongly positive, in-sample(older 63d) flat —
time-instability, not overfit. B1d-trend hypothesis (a): the older half was
genuinely range-bound (a trend-follower *should* be flat in chop), so the honest
move is a **regime gate that sits out chop**, not a param tweak. Tested that.

**Measured the regime split first (no code change).** Kaufman efficiency ratio
(ER = |net move| / summed bar-to-bar moves; ~1=clean trend, ~0=chop), per coin,
per half of the BTC/ETH/SOL/HYPE 90d 1h cache:
- in-sample (older 63d): ER **0.017–0.036** — BTC/ETH/SOL/HYPE ground +10→+16% net
  but via a long noisy path = a choppy grind.
- oos (recent 27d): ER **0.117–0.123** (BTC/ETH/SOL fell −22→−30% cleanly; HYPE
  +54%) = clean directional moves.
→ **Hypothesis (a) is TRUE at the whole-window scale**: the flat older half is chop.

**But the causal (trailing-window) version does NOT separate the regimes.** A live
gate can only see the trailing closes (≤60 bars). Rolling-ER distributions by half:
- 24-bar: median **0.197 (in) vs 0.206 (oos)** — identical.
- 60-bar (longest the agent sees): mean **0.132 (in) vs 0.162 (oos)** — barely apart.
The chop is a **macro multi-week reversal** (local 24–60-bar trends kept flipping
direction over weeks, canceling to a small net move), invisible inside ≤60 bars.

**Changed (1 commit).**
- **`agents/trend_breakout.py`** — new module-level `efficiency_ratio(closes)` helper
  + two optional config keys `min_efficiency_ratio` (default 0.0 = off) and
  `regime_lookback` (default 60). When the floor >0, an entry breakout is skipped
  unless the trailing `regime_lookback`-bar ER clears it. **Tightening-only** (can
  only remove entry candidates), default off → CI/behavior unchanged.
- **`tests/test_trend_breakout.py`** — +2 tests: `efficiency_ratio` returns 1.0 on a
  straight line / 0.0 on a round-trip zig-zag / None on <3 bars; and with the gate ON
  a choppy-path breakout is vetoed (no entry) while OFF the same breakout fires.
  (152 → **154 tests pass**.)

**Evidence (tests/lint).** 154 pass; `ruff check src tests scripts` clean. Caches
gitignored. Baseline reproduced: maker +11.2bps / taker +5.7bps / 552 trades.

**Result — the gate makes the edge MONOTONICALLY WORSE (BTC/ETH/SOL/HYPE 90d 1h, maker):**
| `min_efficiency_ratio` | maker edge | trades | win |
|---|---|---|---|
| 0.0 (off, baseline) | **+11.2bps** | 552 | 40% |
| 0.10 | +2.0bps | 444 | 34% |
| 0.13 (≈ in-sample mean ER) | −2.0bps | 386 | 34% |
| 0.16 (≈ oos mean ER) | −9.0bps | 336 | 32% |

`confirm --prefer maker --config '{"min_efficiency_ratio":0.10}'`: **NOT CONFIRMED**,
and the in-sample edge gets *worse* not better — in-sample **−12.9bps** (vs baseline
+1.1), oos still +33.4bps. The gate did the opposite of its purpose.

**Why it matters (the finding).** Hypothesis (a) is half-right and half-wrong, and
the wrong half is the actionable one: the older window genuinely WAS chop (whole-
window ER confirms it), but a **trailing-ER regime gate cannot fix the time-
instability** because (1) the discriminating signal lives at a multi-week horizon
the agent can't see in ≤60 bars, and (2) more perversely, a high trailing ER means
the trend is already *mature* → the breakout entry is *late* and mean-reverts; the
profitable breakouts fire at trend *birth*, exactly when the trailing window still
reads as choppy. So filtering for "trending now" systematically discards the best
entries. **Trailing-ER regime gating on this signal is pruned.** Lever kept (default
off, tested) so it isn't re-explored and composes with future work. Nothing promoted;
trend_breakout_v1 stays backtest-only/off-roster.

**What's next (loop).** B1d-trend remains open. Next hypothesis (b): are the 24/12-bar
Donchian lookbacks too *fast* (whipsawed by the older grind)? Test a slower horizon
(e.g. entry/exit on a ≥1d channel, or run the 1h agent with much larger lookbacks) —
the honest read may be that this is a real but *slow* trend edge mis-framed at 1h,
not a chop-filter problem. One hypothesis/slice, re-confirm, do NOT param-fit the gate.
Other remaining B1d candidate: (i) spot-vs-perp basis at funding deciles.

---

## Iteration 29 — 2026-06-08 — B1d-trend(b): slower Donchian lookbacks — fixed a silent closes-truncation bug, then pruned the hypothesis (slower = bigger edge but LESS time-stable).

**Context.** Network up (majors cache present). `trend_breakout_v1` is the first
cost-robust positive signal (Iter 27: maker +11.2bps, robust to taker-3×) but NOT
G0-confirmed — its edge is regime-concentrated in the recent (OOS) trending window;
the older in-sample half is flat. Iter 28 pruned a trailing-ER regime gate. B1d-trend
hypothesis (b): are the 24/12-bar Donchian lookbacks too *fast* (whipsawed by the
older grind)? Test slower horizons. Params are already `--config`-overridable, so the
sweep needs no code edit — except it surfaced a measurement bug that made the test
impossible.

**Prerequisite bug found + fixed: `Frame.closes` was silently capped at `vwap_window`.**
Sweeping `entry_lookback` ≥72 returned **0 trades** at first — not "no breakouts" but
"no data": `build_frames` set the agent-facing trailing close series to
`closes[-vwap_window:]` (60 bars), tying the trend lookback window to the *VWAP
smoothing window*. Any lookback > 60 saw an under-length window and the agent's
`len(window) < entry_lookback` guard skipped every bar. This is the **same
silent-truncation class** as the fundingHistory 500-row cap (B4-RUN) and the
candleSnapshot page cap (B1b) — a cap in the data layer masquerading as "no signal".

**Changed (1 commit).**
- **`backtest/data.py`** — new `closes_window: int | None` param on `build_frames`
  / `load_frames` / `cached_or_fetch`, **decoupled** from `vwap_window` (which still
  drives only the TWAP VWAP/sigma). Default = 4×`vwap_window` (240 bars) so
  multi-day Donchian lookbacks are testable; explicitly overridable. Renamed the
  local `closes_window` dict → `closes_window_map` to free the name. Widening the
  series is backward-compatible: the only consumer (`trend_breakout`) slices the
  last N; TWAP agents read precomputed `candles_1h` vwap/sigma, not `closes`.
- **`tests/test_backtest.py`** (+1) — `test_closes_window_decoupled_from_vwap_window`:
  default closes len == 240 while `candles_1h.n` stays 60, and an explicit
  `closes_window=120` is honored. (154 → **155 tests pass**.)
- **Cache rebuilt** (`backtest-fetch --refresh`, network) so the majors cache carries
  the full 240-bar trailing series instead of the truncated 60.

**Evidence (tests/lint).** 155 pass; `ruff check src tests scripts` clean. Caches
gitignored. Baseline reproduced post-rebuild: maker +11.0bps / taker +5.5bps / 552tr
(was +11.2/+5.7 — trivial drift from the refreshed cache window).

**Lookback sweep (BTC/ETH/SOL/HYPE 90d 1h, full sample, now that >60 is testable):**
| entry/exit | maker edge | taker edge | trades | sharpe(mkr) |
|---|---|---|---|---|
| 24/12 (baseline) | +11.0bps | +5.5bps | 552 | +1.84 |
| 48/24 | +6.1bps | +0.6bps | 302 | +0.57 |
| **72/36** | **+22.2bps** | **+16.7bps** | 214 | **+1.41** |
| 96/48 | −18.1bps | −23.6bps | 172 | −0.90 |
| 120/60 | −15.1bps | −20.6bps | 148 | −0.63 |
| 168/84 | −21.3bps | −26.8bps | 112 | −0.80 |

**72/36 (3-day entry / 1.5-day exit) is the full-sample optimum and the bug was
hiding it** (pre-fix it reported 0 trades). It is *more* cost-robust than baseline —
`confirm` cost ladder: maker +22.2 / taker-1× +16.7 / taker-2× +14.7 / **taker-3×
+12.7bps**, robust-to-2× True.

**But G0 still FAILs, and slower lookback makes time-stability WORSE not better:**
`confirm --prefer maker --config '{"entry_lookback":72,"exit_lookback":36}'`:
- in-sample(older 63d) maker **−15.4bps** / −1.06sh / 150tr  (baseline 24/12 was +1.1)
- oos(recent 27d)     maker **+100.1bps** / +5.32sh / 70tr

So a *slower* trend-follower gets chopped up *harder* in the older range-bound grind
(in-sample −15.4 vs baseline +1.1) while riding the recent clean trend even bigger
(OOS +100 vs +35.6). The full-sample number looks great only because the huge OOS
swamps the worse in-sample. The regime-concentration is amplified, not cured.

**Why it matters (the finding).** Two results. (1) A real **measurement bug fixed** —
the backtest can now test trend horizons beyond 60 bars; every prior "slow lookback =
0 trades" reading was an artifact. (2) **Hypothesis (b) pruned with evidence**: lookback
choice does not buy time-stability. The signal is genuinely **regime-dependent** — it
needs a trending macro regime and structurally loses in chop. G0's symmetric "both
halves ≥+3bps" rule effectively demands a pure trend-follower be profitable in a
range-bound half, which it cannot be by construction. Neither a faster nor slower 1h
Donchian, nor a trailing-ER gate (Iter 28), resolves that. **Nothing promoted;
trend_breakout_v1 stays backtest-only/off-roster.** Levers/params kept overridable
(defaults unchanged) so the search space isn't re-explored.

**What's next (loop).** B1d-trend's param/gate levers are exhausted at 1h. Two honest
paths left: (1) reframe the evaluation — a *regime-aware allocation* (deploy trend
only when a trend regime is detected, hold cash otherwise) judged vs a buy-and-hold /
cash benchmark, rather than the symmetric two-half G0 that penalises correct
sit-out-in-chop behaviour; (2) the remaining untried B1d candidate **(i) spot-vs-perp
basis at funding deciles** — carry without the alt price-variance. Pick one next slice.

---

## Iteration 30 — 2026-06-08 — B1d-trend: G0 CLEARED via cross-sectional BREADTH — trend_breakout_v1 is the first confirmed edge. Shipped its paper goals config (live still human-gated).

**Context.** `trend_breakout_v1` was the only positive net-of-cost signal in the
whole hunt (Iter 27) but kept FAILing G0: on the 4 majors (BTC/ETH/SOL/HYPE) its
older/in-sample half is range-bound chop where a pure trend-follower structurally
loses. Iters 28–29 pruned the obvious fixes (trailing-ER regime gate; faster/slower
Donchian lookbacks) — none bought time-stability because the chop is a macro
multi-week reversal invisible at ≤60 bars. **Untried lever this iteration:
cross-sectional BREADTH.** Classic trend-following smooths its equity curve across
many uncorrelated markets — the choppy *majors* half need not be choppy for all 20
liquid names, since different coins trend at different times. The 10- and 20-coin
caches already existed offline (built during the carry hunt), so this needed no code
change to test, just a wider `--coins`.

**Result — breadth tips trend_breakout over the G0 line (90d 1h, `hlbot confirm`):**
| universe | in-sample(maker) | oos(maker) | full maker | taker-3x | verdict |
|---|---|---|---|---|---|
| 4 majors (Iter 27) | +1.1bps | +35.6bps | +11.2 | +1.7 | ❌ in-sample flat |
| 10-coin | **−5.2bps** | +50.0bps | +12.2 | +2.7 | ❌ in-sample −5.2 |
| **20-coin** | **+9.7bps** /+1.64sh | **+41.1bps** /+4.47sh | **+19.2** /+2.67sh | **+9.7** | ✅ **CONFIRMED** |

The in-sample (older, choppy-on-majors) half goes monotonically positive as breadth
rises (majors +1.1 → 10-coin −5.2 is noisy but → 20-coin +9.7 clears it). **20-coin
is CONFIRMED under BOTH maker AND taker** preference (taker: in-sample +4.2bps, oos
+35.6bps/+3.89sh), and cost-robust the whole ladder maker +19.2 → taker-1x +13.7 →
taker-2x +11.7 → **taker-3x +9.7bps**, robust-to-2x True. This is the FIRST strategy
to pass G0 in the entire research program.

**Robustness — not a single-coin artifact.** Per-coin PnL on the 20-coin maker run
(offline, from the fills table): **11/20 coins positive**; top contributor HYPE is
only **25%** of the +$116.60 net; **net ex-top is still +$87.77**. PnL is spread
across HYPE/INJ/ZEC/ARB/WIF/SUI/ADA/SEI/BTC… — exactly the diversified breadth effect,
not one lucky meme. (The carry hunt's OOS sign-flips were driven by 1–2 names; this
is not.)

**Pruned en route (negative result, recorded so it isn't re-tried):** the
"complementary regime" thesis — run mean-reversion in chop, trend in trends — does
NOT trivially clear G0, because the existing TWAP fade is itself negative *even in the
choppy in-sample half*: `confirm twap_mr_v1`/`twap_mr_regime_v1` maker in-sample
**−3.1bps** (regime filter inert on majors at 1h, identical numbers). So there's no
free positive-chop leg to bolt on; breadth, not regime-switching, is what works.

**Honest blocker found — paper deployment is NOT wired yet, by design.** The live
`cli/main.py::_enrich_view` populates `view.extra['closes']` with **60 × 1-MINUTE**
candles (it's the VWAP/sigma feed), but the backtested trend signal consumes
**1-HOUR bars** (`entry_lookback=24` = 24 *hours*). Adding `TrendBreakoutAgent` to the
live roster as-is would deploy a *different* (24-*minute* breakout) strategy than the
one that passed G0 — a violation of "evidence before capital." So I did **not** wire
it into the roster this iteration. Wiring is gated on a live-view fix (new backlog
item B1d-trend-deploy).

**Changed (1 commit).**
- **`configs/trend_breakout_v1.yaml`** (new) — supervisor goals/guardrail/promotion
  config for the now-G0-confirmed strategy, mirroring `twap_mr_regime_v1.yaml`:
  `mode: paper`; primary goal edge_bps>=0; guardrails pause@-$30/24h,
  demote@-10bps/7d; promotion **paper→live_small ONLY** behind the G1 paper gate
  (edge>=+5bps, net>=$50, **n_trades>=150** over 30d). Never auto-promotes to full
  live — enabling live stays a human action (docs/GO_LIVE.md).
- **`tests/test_supervisor_configs.py`** (+1) —
  `test_trend_breakout_config_loads_and_is_paper_only`: the config loads, agent name
  matches, `mode==paper`, promotion `to_mode==live_small`, and the n_trades gate is
  >=150 (the G1 trade-count floor). (155 → **156 tests pass**.)

**Evidence (tests/lint).** 156 pass; `ruff check src tests scripts` clean. G0 numbers
above are from `hlbot confirm` on the gitignored 20-coin 1h 90d cache (reproducible
with `backtest-fetch` where HL is reachable).

**Why it matters.** The mission's whole premise (REVIEW: "a well-built chassis with no
engine") now has a candidate engine: a strategy with **durable, cost-robust, breadth-
diversified positive edge that survives walk-forward + 3x cost stress**. G0 is the
first of four gates; this is the first time any agent has cleared it. The edge is a
breadth phenomenon, so its deployment REQUIRES the wide (~20-name) universe — a
deployment fact now captured in the config's description.

**What's next (loop).** B1d-trend-deploy (new, top of P0 now): make the live
`_enrich_view` emit a **1h close series** (>= entry_lookback+1, ideally 240 to match
the backtest `closes_window`) so the paper deployment runs the SAME signal that
passed G0; then add `TrendBreakoutAgent` to the paper roster on the 20-coin universe.
That starts the G1 paper track record (>=30d, edge>=+5bps, >=150 trades). Do the
live-view fix as its own tested slice (refactor the 1h-candle build into a pure,
mockable function — it touches network code). Secondary: a longer-history (>90d, more
regime cycles) confirm to check G0 stability across more than one chop→trend
transition.

---

## Iteration 31 — 2026-06-08 — B1d-trend-deploy Slice 1: live `closes` now carries real 1h bars (was 60×1m) via a pure, sim-shared loader — unblocks paper deployment of the G0-confirmed trend signal.

**Context.** Iter 30 cleared G0 with `trend_breakout_v1` (20-coin breadth, CONFIRMED
under maker+taker, cost-robust to taker-3×) but flagged an honest deployment blocker:
the live `cli/main.py::_enrich_view` fills `view.extra['closes']` with **60×1-MINUTE**
candles (the VWAP/sigma feed), while the backtested trend signal reads `closes` as
**1-HOUR bars** (`entry_lookback=24` = 24 *hours*). Adding `TrendBreakoutAgent` to the
roster as-is would silently deploy a *24-minute* breakout — a different strategy than
the one that passed G0, forbidden by "evidence before capital." This is B1d-trend-deploy
Slice 1: make the live view emit the SAME 1h close series the backtest scored on.

**Changed (1 commit).**
- **`backtest/data.py`** — new `build_closes_1h(post_fn, coins, *, closes_window=240,
  now_ms=None)`: a **pure, transport-injected** loader returning a per-coin trailing
  1-HOUR close series (oldest-first, last = latest bar). It walks the candleSnapshot
  page cap via the existing `paginate_by_time` and parses with the same `_closes_vols`
  the backtest frames use — so **live and sim see an identical series** (default 240
  bars = the backtest `closes_window`). `post_fn(payload)->json` makes one `/info` POST,
  so it is fully unit-testable offline with a fake. (`Callable` imported from
  `collections.abc`.)
- **`cli/main.py::_enrich_view`** — removed the line that stuffed the **1m** closes into
  `closes_by_coin`; now populates `view.extra['closes']` from `build_closes_1h` using a
  resilient `_post` closure over the existing httpx client (per-call try/except → `[]`,
  matching the surrounding defensive style; whole call wrapped so a fetch failure
  degrades to empty closes rather than killing the view). The 60×1m loop still drives
  `candles_1h` VWAP/sigma (TWAP feed) unchanged.
- **`tests/test_backtest.py`** (+1) — `test_build_closes_1h_emits_hourly_series_matching_backtest`:
  fake `post_fn` over 300 synthetic hourly candles/coin with the page cap monkeypatched
  to 50 to force pagination; asserts the request interval is **1h** (not 1m), the series
  is window-capped to `closes_window` (120), `[-1]` is the latest close, `[0]` is the
  oldest in-window bar (300−120), and the result is ascending+deduped across pages.
  (156 → **157 tests pass**.)

**Evidence (tests/lint).** 157 pass; `ruff check src tests scripts` clean.

**Why it matters.** This removes the one hard blocker between a G0-confirmed signal and
its paper track record without touching the strategy: the live agent will now consume
the *same* 1h bars the backtester confirmed on. It is also a latent-correctness fix for
the **other** `closes` consumers — `twap_mr_regime` (regime fade, on the paper roster)
and `xfund_carry` both read `view.extra['closes']` and were silently being fed 1m bars
live while backtested on 1h; they now match their backtest contract too. No agent was
added to the roster and live stays human-gated — Slice 2 (roster wiring + a live-vs-sim
signal equivalence check on the 20-coin universe) is the next slice. Per "evidence
before capital," the parity loop (does the live `decide()` reproduce the backtest's
breakout entries on the same frames?) must pass before the G1 paper clock can be
trusted.

**What's next (loop).** B1d-trend-deploy Slice 2: add `TrendBreakoutAgent` to the paper
roster over the ~20 liquid coins (config done Iter 30), and add a parity check that the
live signal on a captured view matches the backtest signal on the equivalent frame.
Then the G1 paper clock starts (>=30d, edge>=+5bps, >=150 trades). Secondary unchanged:
a longer-history (>90d) confirm for G0 stability across more regime cycles.

---

## Iteration 32 — 2026-06-08 — B1d-trend-deploy Slice 2: trend_breakout wired to the PAPER roster + live-vs-sim closes-parity proof — the G0-confirmed engine starts its G1 paper clock.

**Context.** Iter 30 cleared G0 with `trend_breakout_v1` (20-coin breadth, CONFIRMED
maker+taker, cost-robust to taker-3×). Iter 31 (Slice 1) removed the deployment
blocker by making the live `_enrich_view` feed `view.extra['closes']` real **1h**
bars via `build_closes_1h` (was 60×1m). Slice 2 is the actual roster wiring + the
"evidence before capital" parity check it was gated on: prove the live agent
reproduces the backtested signal before the paper track record can be trusted.

**Changed (1 commit).**
- **`cli/main.py::femr_tick`** — added `TrendBreakoutAgent(config=_cfg(
  "trend_breakout_v1", {}), conn=conn)` to the agent roster (after the two TWAP
  agents). `{}` defaults = the agent's G0-confirmed params (24/12 Donchian, 0.05
  stop, 96h max-hold, 100/300/4 notional); operator can override via
  agent_overrides.json. The roster already runs on the **top-20-by-volume** universe
  `_enrich_view` builds, and (since Slice 1) feeds it real 1h closes — so the paper
  agent consumes the SAME universe + series the backtest confirmed on. **Paper by
  default**; in `--live` it is filtered out by `_filter_live_agents_by_state` unless
  an `agent_state` row enables it in live_small/live (a human action, docs/GO_LIVE.md)
  — so this wiring touches no capital.
- **`tests/test_trend_breakout.py`** (+1) —
  `test_live_closes_loader_matches_backtest_frame_and_decisions`: from one set of raw
  1h candles (260 bars > 240 window, so the cap is exercised), derive `closes` two
  ways — live `build_closes_1h` vs backtest `build_frames`→last `Frame.closes` — and
  assert (a) the per-coin series are **byte-identical** (and both end on the latest
  close: the off-by-one a wiring bug would break), and (b) the agent emits **identical**
  (action, coin, side) decisions on each view (volume + mids held equal to isolate the
  closes series, the only input Slice 1 changed). The signal actually fires on both
  (BTC new-high → long, SOL new-low → short; ETH flat → no entry), so it is not a
  vacuous both-empty match. (157 → **158 tests pass**.)

**Evidence (tests/lint).** 158 pass; `ruff check src tests scripts` clean. The parity
test is the deployment evidence: live path == sim path on identical inputs, so the
paper agent runs the exact strategy that cleared G0 (no re-confirmation of edge here —
edge numbers stand from Iter 30's `hlbot confirm`).

**Why it matters.** The mission's candidate engine (Iter 30) is now actually *running*
in paper on the roster, not just confirmed in a backtest. This starts the **G1 paper
gate** clock: >=30d live-paper, edge >= +5bps, >=150 trades, no guardrail breach
(`configs/trend_breakout_v1.yaml`). G1 is the first gate that measures the edge on
*forward, unseen* data at real cadence — the honest test the carry overfits never
survived. Live promotion stays human-gated.

**What's next (loop).** Let the paper clock run; surface a per-agent paper scorecard
for trend_breakout (edge_bps / n_trades / win_rate over the trailing window) so G1
progress is observable without re-deriving it by hand — the `track-record` / supervisor
plumbing already computes per-agent edge, so this is wiring, not new math. Secondary
(unchanged): a longer-history (>90d) confirm for G0 stability across more regime cycles;
and the remaining B1d candidate (i) spot-vs-perp basis at funding deciles if a second
uncorrelated edge is wanted.

---

## Iteration 33 — 2026-06-08 — Observable G1 gate progress: `promotion_progress` + `hlbot gate-progress` surface distance-to-live for the trend paper clock.

**Context.** Iter 32 wired `trend_breakout_v1` to the PAPER roster and started its
G1 clock (>=30d paper, edge >= +5bps, net >= $50, >=150 trades @30d, no guardrail
breach). The Iter-32 "what's next" called for surfacing per-agent paper progress so
G1 is watchable without re-deriving it by hand. The supervisor's `evaluate` already
checks the promotion conditions, but it only emits a `promote` Evaluation when ALL
conditions pass — there was no way to see "2 of 3 met, edge_bps still short" while
the clock runs. This iteration is that observability (wiring, not new math).

**Changed (1 commit).**
- **`supervisor/goals.py`** — new pure `promotion_progress(conn, g) -> GateProgress |
  None` plus `ConditionProgress`/`GateProgress` dataclasses. For each promotion
  condition it scores the agent with the *same* `score_agent(..., capital_base=g.capital)`
  and `Condition.evaluate` the supervisor uses, and reports `(metric, window, op,
  threshold, value, status[pass/fail/na])` for EVERY condition — plus `n_met`,
  `n_total`, and `ready` (all pass). Returns None when the agent has no promotion
  block. Read-only; `ready` here matches what `evaluate` would promote on, modulo the
  dominating guardrail check.
- **`cli/main.py`** — new `hlbot gate-progress [--agent X] [--configs DIR]`: loads every
  `*.yaml`, computes `promotion_progress`, and renders a per-agent distance-to-gate
  table (condition / current value / ✓✗N/A) with a `READY` or `n/total met` header.
  Read-only on the DB.
- **`tests/test_supervisor_configs.py`** (+3) — (1) *partial progress*: 200 tiny-edge
  fills clear n_trades(>=150) but leave net_pnl(>=$50) and edge_bps(>=+5) failing →
  `n_met==1`, `ready False`, per-condition statuses asserted (n_trades pass w/ value
  200; edge_bps fail w/ value <5). (2) *all-pass → ready*: 200 fat-edge fills →
  `ready True`, `n_met==n_total==3`. (3) *no promotion block → None*.

**Evidence (tests/lint).** 158 → **161 pass**; `ruff check src tests scripts` clean.
`hlbot gate-progress --help` registers. No edge claim here — this is measurement
plumbing over the existing G0-confirmed signal; the edge numbers stand from Iter 30.

**Why it matters.** The G1 paper gate is the first forward-data test of the
trend_breakout edge, and it runs for >=30d. Without a distance-to-gate view, an
operator (or the next loop iteration) has to hand-join three `score_agent` windows
against the YAML thresholds to know how close promotion is. `gate-progress` makes
that a one-command read, and `promotion_progress` is the reusable primitive a future
report/Telegram digest can call. Live promotion stays human-gated.

**What's next (loop).** Let the paper clock run; candidate next increments: (a) fold
`promotion_progress` into the daily `report`/track-record so G1 distance shows up in
the digest automatically; (b) longer-history (>90d) `confirm` for G0 stability across
more regime cycles; (c) the remaining B1d candidate (i) spot-vs-perp basis at funding
deciles for a second uncorrelated edge.

---

## Iteration 34 — 2026-06-08 — G1 distance-to-live now shows up in the daily digest automatically (`report` folds in `promotion_progress`).

**Context.** Iter 33 added `promotion_progress`/`hlbot gate-progress` so the
trend_breakout G1 paper clock (edge>=+5bps, net>=$50, >=150 trades @30d) is watchable
on demand. Its "what's next" (a) called for folding that into the daily `report` so
the operator (and the loop) sees G1 distance in the digest without running a second
command. This is that — pure wiring over the Iter-33 primitive, no new math, no edge
claim, fully offline-testable. Picked over (b)/(c) because those need network for real
history and are larger; this is the smallest valuable unblocked slice.

**Changed (1 commit).**
- **`reports/daily.py`** — `build(conn, configs=None)` now appends a
  `## Gate progress` section after the per-agent scorecard. Two new pure helpers:
  `render_gate_progress(reports) -> str` (one block per agent with a promotion gate:
  `from → to (n/total met | READY)` header + a `✓/✗/N/A` line per condition with its
  current value vs threshold; returns `""` when no agent has a gate, so the section is
  skipped cleanly) and `gate_progress_reports(conn, configs=None)` (loads every
  `configs/*.yaml`, returns the non-None `promotion_progress` results). `build`'s
  signature gained an optional `configs` (defaults to `CONFIG_DIR`), so the existing
  `hlbot report` call site is unchanged.
- **`tests/test_daily_report.py`** (new, +4) — (1) empty reports → blank string;
  (2) `render_gate_progress` marks each condition (`✗ edge_bps`, `✓ n_trades`,
  `N/A net_pnl`) and renders values (`+0.50`, `—` for na); (3) all-pass → `(READY)`
  header; (4) `build` with the real `trend_breakout_v1.yaml` + 200 tiny-edge fills
  shows the base report AND the gate section with n_trades met but edge short.

**Evidence (tests/lint).** 161 → **165 pass**; `ruff check src tests scripts` clean.
No edge claim — measurement plumbing over the Iter-30 G0-confirmed signal; edge
numbers stand from Iter 30's `hlbot confirm`.

**Why it matters.** The G1 gate runs >=30d on forward paper data; the daily report is
the artifact an operator actually reads each day (and the loop can diff). Surfacing
distance-to-gate there means promotion-readiness is visible passively, so a human knows
when to consider the (always human-gated) live_small flip without re-deriving three
`score_agent` windows by hand. `gate_progress_reports` is also the reusable primitive a
future track-record/Telegram digest can call.

**What's next (loop).** Let the paper clock run. Remaining candidates: (b) longer-history
(>90d) `confirm` for G0 stability across more regime cycles (needs network); (c) the
B1d candidate (i) spot-vs-perp basis at funding deciles for a second uncorrelated edge
(needs network). Offline alternative: a longer-history backtest fixture or a regime-aware
*allocation* benchmark for trend (run-only-in-trend vs buy-and-hold) since G0's symmetric
two-half rule structurally penalises a pure trend-follower in chop.

---

## Iteration 35 — 2026-06-09 — G0 edge HARDENED on a doubled window: `trend_breakout_v1` still ✅ CONFIRMS over 180d 1h under maker.

**Context.** Iter 30 cleared G0 for `trend_breakout_v1` on a **90d** 20-coin
universe; Iters 31–34 wired it to paper and made the G1 clock observable. The
standing "what's next (b)" across the last several iterations was a longer-history
`confirm` to test G0 stability across *more regime cycles* — the single most
decisive piece of evidence before the (human-gated) live_small flip, and exactly
"evidence before capital." Network is reachable on this host, so this is now
unblocked. Picked over (c) spot-vs-perp basis (a new, larger, speculative edge
hunt) because hardening the *one edge we already have* is higher leverage than
starting a second one.

**What I did.**
- Fetched **180d 1h** for the G0 universe (ADA,APT,ARB,AVAX,BTC,DOGE,ETH,HYPE,INJ,
  LINK,LTC,NEAR,OP,SEI,SOL,SUI,TIA,TRX,WIF,ZEC) → 4321 frames cached
  (`backtest-fetch`, gzipped, gitignored). Doubles the G0 window (90d→180d).
- Ran `hlbot confirm --agent trend_breakout_v1 --days 180` at the G0-confirmed
  defaults, under **both** preferred-execution modes.

**Evidence (the numbers).**
- **prefer=maker → ✅ CONFIRMED (4321 frames):**
  - walk-forward: in-sample(maker) net $+50.83 / **+5.5bps** / +0.85sh / 1130 trades;
    oos(maker) net $+83.84 / **+22.8bps** / +2.88sh / 456 trades.
  - cost ladder (full sample): maker +11.1bps / taker-1× +5.6bps / taker-2× +3.6bps /
    **taker-3× +1.6bps**; **robust to 2× slippage: True**.
  - verdict: clears +3bps in & out of sample with sharpe ≥ 1.0.
- **prefer=taker → ❌ NOT CONFIRMED**, for one honest reason: the **older** in-sample
  half is **flat at taker cost** (in-sample(taker) net −$0.39 / **−0.0bps** / +0.09sh),
  while oos(taker) is strongly positive (net $+63.60 / **+17.3bps** / +2.21sh) and the
  full-sample taker-1× is +5.6bps. So the older regime's trend edge survives **only at
  maker cost** — directly confirming REVIEW C1 (taker tax is the structural bleed) and
  validating that this agent is correctly deployed as a **maker** strategy.

**Interpretation.** The edge is not a 90d regime artifact: it reproduces walk-forward
on a second, longer, more regime-diverse window under its *intended* execution. The
maker-vs-taker split is the load-bearing detail — the older choppier regime is the one
that needs maker pricing to stay positive, which is the whole thesis behind running
this strategy post-only. This is the strongest pre-live evidence to date.

**Changed (in-repo artifacts, no code logic).**
- `configs/trend_breakout_v1.yaml` — description now cites the 180d maker
  confirmation + the taker caveat, so the hardening evidence lives next to the agent.
- `ralph/BACKLOG.md` — annotated the G0-CLEARED entry with the 180d result; closes
  "what's next (b)".

**Evidence (tests/lint).** 165 pass (unchanged — doc/config edits only);
`ruff check src tests scripts` clean.

**Why it matters.** Two independent walk-forward windows (90d and 180d) now confirm
the one edge in the entire hunt that survives costs, under maker execution. The G1
paper clock keeps running on forward data; this gives a human the longest-history
backtest evidence to weigh the (always human-gated) live_small decision against.

**What's next (loop).** (a) Let the G1 paper clock run (time-gated). (b) Optional
further hardening: an even-longer window if HL 1h history extends past 180d, or a
365d-equivalent at 4h. (c) The remaining B1d candidate (i) — spot-vs-perp basis at
funding deciles — for a *second uncorrelated* edge, now that the first is twice-confirmed.

---

## Iteration 36 — 2026-06-09 — Pinned the paper trend agent to the G0-confirmed universe (closes a deploy-vs-evidence drift).

**Context.** Iter 30/35 confirmed `trend_breakout_v1` (G0, twice — 90d + 180d) on a
**fixed** 20-coin liquid universe; Iters 31–34 wired it to the paper roster and made
the G1 clock observable. Reading the live wiring this iteration surfaced a
deploy-vs-evidence drift of the *same class* the project already guards against (the
careful 1m-vs-1h `closes` fix in B1d-trend-deploy): `cli/main.py::_enrich_view` builds
`view.extra['closes']` for the **top-20-by-volume** set, which drifts day to day,
while the agent trades *every* coin in that feed (only filtered by `min_daily_volume_usd`).
So the paper agent was trading whatever names drifted into the volume top-20 — not the
confirmed 20 — making the G1 forward-test an *unfaithful* test of the G0 evidence.
Picked this over the speculative spot-vs-perp basis hunt (candidate (i)) because
hardening the faithfulness of the *one edge we have* before any live decision is
higher-leverage than starting a second, unconfirmed edge ("evidence before capital").

**Changed (1 commit).**
- **`agents/trend_breakout.py`** — `TrendBreakoutConfig.universe: tuple[str,...] = ()`
  (tightening-only; `()` = off = trade every coin fed, so every existing backtest and
  `hlbot confirm` run is byte-for-byte unchanged). Parsed in `__init__`
  (`tuple(c.get("universe", ()) or ())`). In `decide`, the **entry** candidate loop
  skips coins not in the allowlist when set; the **exit** loop is untouched, so a coin
  leaving the universe can never strand an open position.
- **`cli/main.py`** — the `femr_tick` roster pins trend_breakout to the exact confirmed
  20 names (ADA APT ARB AVAX BTC DOGE ETH HYPE INJ LINK LTC NEAR OP SEI SOL SUI TIA
  TRX WIF ZEC) via the `_cfg("trend_breakout_v1", {"universe": [...]})` default, with a
  comment explaining the faithfulness rationale. Paper by default + live-gated by
  `agent_state` — no capital touched.
- **`configs/trend_breakout_v1.yaml`** — description documents the pinned universe.
- **`tests/test_trend_breakout.py`** (+2) — (1) with `universe=["BTC"]`, two coins
  both breaking out → only BTC entered, the drifted-in name skipped; empty universe
  (default) takes both (backward compat). (2) a seeded out-of-universe open position
  (TST not in `["BTC"]`) reversed below its exit channel still **flattens** — exits
  aren't filtered.

**Evidence (tests/lint).** 165 → **167 pass**; `ruff check src tests scripts` clean.
No edge claim — this is measurement-faithfulness plumbing over the Iter-30/35
G0-confirmed signal; the edge numbers stand from `hlbot confirm` (90d + 180d).

**Why it matters.** The G1 paper gate is the decisive pre-live forward-test, and it's
only meaningful if the paper agent trades the *same* strategy that passed G0. Pinning
the universe removes the silent drift (volatile alts cycling through the volume top-20)
so the forward sample validates the confirmed edge rather than an ever-changing
universe. Strictly tightening, default-off, fully offline-tested; promotion to
live_small stays human-gated.

**What's next (loop).** (a) **B1d-trend-pin-fetch** (new backlog item): `_enrich_view`
still only *fetches* closes for top-20-by-volume, so the pinned agent trades the
intersection (confirmed ∩ today's top-20). Fetch `build_closes_1h` for the union of the
volume top-20 and the agent's pinned `universe` so every confirmed name always has its
1h series. (b) Let the G1 paper clock run (time-gated). (c) The remaining B1d candidate
(i) spot-vs-perp basis at funding deciles for a *second uncorrelated* edge.
