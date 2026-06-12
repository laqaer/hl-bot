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
