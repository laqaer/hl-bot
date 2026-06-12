# Backlog — prioritized

The loop works this top-to-bottom, skipping blocked items. `[ ]` = todo,
`[x]` = done, `[~]` = in progress, `[B]` = blocked (reason in note).
Keep it ruthlessly prioritized: the top item should always be the highest-leverage
*unblocked* thing. Add new findings as you discover them.

## P0 — find an edge (the whole point)

- [x] **B1 — Quantify the taker tax on every agent.** Done (Iteration 20, network
  reachable on this host). 90d 1h, BTC/ETH/SOL/HYPE: twap_mr_v1 taker −10.0bps →
  maker −4.6bps (tax ≈5.4bps); twap_mr_regime_v1 −9.8→−4.5 (regime filter nearly
  inert at 1h on majors); femr_v1 0 trades on majors (funding too small). Numbers
  in `PROGRESS.md`. _Was blocked on network; now unblocked here._
- [x] **B1a — Offline history cache.** Done: `hlbot backtest-fetch` +
  save/load/cached_or_fetch under `data/backtest_cache/` (gzipped JSON,
  gitignored); `hlbot backtest --cache` runs without network. (Iteration 2.)
- [x] **B2 — Maker (post-only) execution.** Closed (audit, Iter 63): every
  remaining piece shipped elsewhere — cross-tick fill reconciliation (B2b),
  book-aware pricing (B-book), instant fill detection (B10b/B10c), honest
  maker backtests (B-MAKERFILL/B-FILL2). The one open thread — actually
  routing live entries through maker — is B-MAKER-LIVE (evidence-blocked).
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
- [x] **B4-RUN — Confirm carry strategies on real history.** Run on a 10-coin
  liquid-alt universe incl. ZEC (Iteration 20, network up). **NOT confirmed (FAIL
  G0):** xfund_carry_v1 maker −4.3bps full-sample but OOS-maker +3.4bps/+0.60sh
  (52 trades) — a faint recent-regime signal, in-sample −43.7bps; funding_carry_v1
  maker −111bps (single-name carry run over by price on volatile alts); femr_v1
  maker −27.9bps. Numbers in `PROGRESS.md`. **Prerequisite found+fixed:** HL
  `fundingHistory` 500-row cap silently truncated >20d windows → all prior carry
  backtests were invalid (saw only oldest ~20d of carried-forward funding); fixed
  via `paginate_by_time`. **Next:** xfund is closest — try wider universe / longer
  hold / tighter entry; do NOT promote (still negative).

- [x] **B1b — Paginate candle fetch too.** Done (Iter 21): `fetch_candles` now
  walks the candleSnapshot ~5000-row per-call cap via `paginate_by_time(time_key=
  "t", page_limit=CANDLE_PAGE_LIMIT)`, so fine intervals (1m/5m) over long windows
  no longer silently truncate the recent bars the way `fundingHistory` did.
  Unit-tested offline (fake httpx, shrunk cap).
- [x] **B1c — Edge hunt on corrected funding. CLOSED (Iter 47): every lever
  pruned; carry stays evidence-gated OFF.** Final two hypotheses (numbers in
  PROGRESS): (4) *beta-neutral sizing* (`beta_neutral` lever, shipped+tested in
  an interrupted Jun-8 session, A/B'd Iter 47) IS a real variance lever —
  maker Sharpe +1.21→+2.01→+2.31 and maxDD halves as the shrink deepens — but
  it cannot change the sign of the carry: walk-forward still FAILS (in-sample
  −19bps). Keep default OFF; flip it only if xfund ever earns a book.
  (5) *lower cadence* is WORSE at 4h (−4.3→−7.1bps maker with honest in-bar
  funding integration; beta combo −18.6) and *unprovable* at 1d: 14 trades/90d
  by construction (funding API retention caps the sample), and the seductive
  "+177bps CONFIRMED" 1d print was a thin-sample gate false-positive — 2
  in-sample trades — now structurally rejected by the confirm `min_trades`
  floor this iteration added. Meta-finding: baseline swung −4.3 → +11.3bps
  maker on a 4-day window roll (all profit in the June funding-dispersion
  pocket; walk-forward IS −43.7); variance ≫ signal. Tooling done earlier: `--config '{json}'` overrides (`parse_agent_config` + `_backtest_factories`,
  now take `--config '{json}'` overrides (`parse_agent_config` + `_backtest_factories`,
  tested) so xfund params sweep without code edits. **Two hypotheses pruned (Iter 22,
  numbers in PROGRESS):** (1) *tighter entry* makes xfund WORSE not better
  (2bp/hr → −40.5bps maker / 18 trades / 33% win; ≥3bp/hr → 0 trades, funding caps
  ~2.7bp/hr); (2) *wider universe* (10→20 liquid alts) also WORSE (−4.3 → −12.1bps
  maker, OOS −23.6bps) — the Iter-20 narrow-10-coin OOS +3.4bps was a sample
  artifact, not robust. Root cause: high-|funding| alts are volatile *because*
  funding is extreme, so price variance buries the carry whether you concentrate
  OR diversify. `top_k>2` inert (eligible legs/side cap out). **Best config in book
  is still the loosest baseline at −4.3bps maker — negative.** **Third hypothesis
  pruned (Iter 23):** *cut the rotation churn* via `hold_while_eligible` (hold a leg
  while its funding stays eligible+correct-side instead of exiting on rank rotation)
  cut trades 62→36 as designed but made it WORSE (−4.3 → −17.6bps maker / −1.57
  net). Root cause: the churn wasn't waste — it concentrates the book into the
  *highest*-funding names; holding rank-rotated lower-funding legs collects less
  carry while still eating price variance over a longer hold. The config lever is
  kept (default off, tested) so the dead end isn't re-explored. Remaining to try:
  funding-decile *neutralised-by-beta* cross-section (dollar-neutral ≠ market-neutral
  when shorts are higher-beta alts), or a different (lower) cadence so funding accrual
  outweighs per-bar price noise. Evidence-gated; nothing promoted.

## P1 — honest measurement (so the supervisor can trust itself)

- [x] **B-PNL-SPLIT — health's `pnl_24h` (and its loss floor) judged the whole
  account, not the bot.** Done (Iter 88; found Iter 87 live: health printed
  pnl_24h −$325.80 while the bot's own 24h book was +$0.97 — the difference
  was `agent='manual'` fills, the operator trading the same account).
  `assess_health` now splits 24h PnL bot vs account in one query (bot = agent
  NOT NULL and ≠ 'manual'; `unknown:` prefixes are bot-tagged cloids → bot;
  NULL = pre-attribution legacy → not ours) — the `daily_loss_floor` crit
  keys on BOT PnL only, both numbers print (`bot $+2.84 (account $-323.93,
  manual $-326.77)` live-fired on the deploy DB — the incident, rendered
  honestly). The floor is now ARMABLE: `hlbot health --daily-loss-floor` /
  `HLBOT_DAILY_LOSS_FLOOR` env (it was hardwired unarmed at −1e9, the crit
  unreachable in deployment); malformed env refuses to run (missed dead-man
  ping pages) rather than silently disarm. Track record: per-agent tables
  verified already split (manual/unknown excluded); headline account section
  CANNOT split (equity_snapshots are address-wide) so all three exports carry
  an explicit shared-account caveat (`ACCOUNT_NOTE`). Operator: arm via
  /etc/hl-bot/env (deploy/README §Monitoring), e.g. −3%/day ≈ −$20 today.
- [x] **B-BOTREC — Bot-only composite record headlines the track record +
  funding-consistent daily series.** Done (Iter 92): the track record's
  headline was the account-wide equity curve — on the shared address it is
  dominated by the operator's manual book (live-fired: bot composite
  **+$201.74 / 10d, sharpe(d) +6.12, maxDD$ −$77.65, 1014 trades** while the
  account headline reads −5.0%), and there was no aggregate bot number an
  allocator could underwrite. Now `_bot_record`: every bot-attributed fill
  (B-PNL-SPLIT's criterion: agent NOT NULL ≠ manual; unknown:* included)
  plus size-weighted attributed funding, bucketed by UTC day → net/fills/
  funding split, sharpe(d), maxDD$, cumulative-PnL curve (day-end points,
  zero anchor) rendered FIRST in md/html/json with its own SVG + basis note.
  Consistency fix found while there: per-agent `sharpe_daily`/`maxDD$` were
  computed fills-only while the net column beside them includes attributed
  funding (and the paper section's series includes modeled funding) — a
  funding strategy's revenue line was invisible to exactly the columns that
  judge its risk; `_agent_daily_pnl` now folds attributed funding onto its
  day. 4 new tests (manual/NULL excluded + unknown:* included, manual's
  funding share stays out, funding-day drawdown shows, empty-book render).
- [x] **B-CALMAR — Account calmar prints absurd compounding on short
  windows.** Done (Iter 93): `MIN_CALMAR_DAYS = 30` in `scoring/metrics.py`
  gates BOTH calmar sites — the account arm (`score_agent` `_account`:
  `len(rets) >= 30` daily returns) and `_daily_pnl_drawdown` (per-agent +
  paper cards share it) — calmar is None below 30 daily observations;
  drawdown/sharpe report regardless. Renderers already degrade None to "—".
  No calmar-keyed gate exists anywhere (configs/supervisor grepped) so this
  is pure report honesty. Backtest engine `_curve_stats` deliberately left
  alone (research output, sample length always printed beside it).
  Live-fired read-only on the deploy DB: account all-window calmar
  +7.9e45 → None (4 snapshot days), maxDD/sharpe intact.
- [x] **B-PAPER — Make the paper book exist + paper/live book separation.** Done
  (Iter 37). Found while scoping B-EDGE2a: paper `femr_tick` NEVER logged
  place/flatten (gap dates to the original femr_tick — `defer_exec_logging`
  defers to an execution loop paper mode never reaches), so "paper trading"
  produced no book at all and paper agents couldn't track their own positions
  (the twap_mr_regime "paper pilot" accumulated zero evidence). Now: paper ticks
  log exec decisions at gather time (is_paper=1), and every decision-log replay
  is book-aware — agents replay the book matching the tick mode
  (`Agent.paper_book`, set by `gather_decisions`), `bot_owned_coins` /
  `coin_in_cooldown` default to the LIVE book, reconcile is live-gated +
  live-book-only — so a paper row can never reclassify a manual position as
  bot-owned, gate a live entry, or trigger a phantom live flatten. femr also no
  longer re-enters a coin its paper replay holds. Live-fire verified (scratch
  DB, 3 real paper ticks): entries logged is_paper=1, tick-2 shows
  `bot-owned: [TRX, XMR]`, no duplicate entries. Operator doc: deploy/README
  §Paper book (use `HLBOT_DB=data/hlbot_paper.sqlite` for a paper loop beside
  a live one).
- [x] **B-PAPER2 — femr paper-EXIT fidelity.** Done (Iter 40): paper ticks
  synthesize `view.extra["live_positions"]` from the paper-book replay
  (`runtime.synthesize_paper_positions` — same dict shape the backtest engine
  synthesizes from its own book: szi/entry from the log, value/uPnL marked at
  the current mid, liq_px=0 disables proximity checks) so femr's section-1
  exit ladder (stop/TP/max-hold/funding-normalized) runs on the paper book
  too; the logged paper flatten then closes the book. Live ticks unchanged
  (exchange-truth view, live book only). Live-fire verified: seeded +2% paper
  BTC long → FEMR-EXIT TAKE-PROFIT at the real mid, slot rotated into a fresh
  funding entry, round trip realized in `hlbot score --paper`.
- [x] **B-PAPER3 — paper-book scorecard.** Done (Iter 39): `scoring/paper.py`
  replays the paper decision book (is_paper=1 place/flatten, same replay
  semantics as the agents' own `_position_state`) into synthetic fills under
  the backtester's `CostModel` (taker fees + slippage → comparable to G0
  numbers) and aggregates the same `Scorecard` shape as `score_agent`
  (windows, win stats, daily Sharpe, capital-based DD). `hlbot score --paper`
  prints the cards + still-open paper positions. NOT auto-promotion. Limits
  (by design, follow-ups below): funding_pnl=0, realized-only (no
  mark-to-market on open positions).
- [x] **B-PAPER3a — paper funding accrual.** Done (Iter 41): `hlbot score
  --paper` fetches HL funding-rate history over each paper hold
  (`paper_funding_spans` → `fetch_funding_history`, per-coin error isolation,
  `--no-funding` opt-out) and models accrual per hourly event with the
  engine's `-signed×notional×rate` (marked at entry mid — offline proxy for
  the hourly mark), window-filtered by event time like live
  funding_payments. femr's paper revenue line is now visible; `funding`
  column added to `hlbot score` output. Live-fire verified on real rates.
- [x] **B-PAPER3b — paper section in the track record.** Done (Iter 42):
  `build_track_record` grows a `paper_agents` section (per-agent paper cards
  via `score_paper_agent` + gap-filled `paper_daily_pnl` so sharpe(d)/maxDD$
  are computed identically to the live columns), rendered as "Paper agents
  (NOT live)" in md/html/json with an explicit forward-test disclaimer.
  Paper-only agents no longer leak into the live table as zero-trade rows
  (`list_agents` sees `agent_decisions`); an agent with both books shows in
  both. `hlbot track-record` fetches modeled funding for the paper section
  by default (`--no-paper-funding` opt-out; per-coin failure degrades to 0),
  sharing `_fetch_paper_funding` with `score --paper`.
- [x] **B-PAPER3c — goal evaluation on paper cards (pause/demote ONLY).** Done
  (Iter 43): `evaluate` scores an agent from its paper book when its
  *effective* mode (agent_state row > YAML `mode:`) is paper AND paper rows
  exist; guardrails (pause/demote/alert) fire on paper evidence, audit rows
  are `[paper]`-tagged. Promotion from paper cards is downgraded to an
  informational "promotion-ready … human-gated, not applied" evaluation
  (action=none) AND `run_once` refuses any paper-sourced `promote` (defense
  in depth). `hlbot supervisor` models paper funding by default
  (`--no-paper-funding` opt-out) so femr isn't judged on funding=0 cards.
  Bonus fix: the promotion mode-check now uses the effective mode, so an
  agent already promoted in agent_state can't be re-promoted off a stale
  YAML `mode:`. Live-fire verified both directions on scratch DBs.

- [x] **B-PAPER3d — Mark open paper positions to market.** Done (Iter 61):
  `mark_paper_positions` (pure, tested) marks each open paper position at the
  current mid net of modeled exit costs — upnl is EXACTLY the `closed_pnl −
  fee` a replay flatten at that mid would realize (invariant pinned by test),
  so card-realized + open-uPnL = flattened-now book value with no double
  count when the position later closes. `hlbot score --paper` fetches mids
  (one allMids call, `--no-mark` opt-out, fetch failure degrades to unmarked)
  and shows mark_px/upnl columns + a per-agent open-uPnL summary line. Cards
  stay realized-only by design (marks beside, never folded in). Closes the
  blind spot where a multi-day breakout hold deep underwater showed a clean
  realized card.
- [x] **B-PAPER3e — Open-position uPnL in the track-record paper section.**
  Done (Iter 62): `build_track_record(paper_mids=)` marks each paper agent's
  open book via `mark_paper_positions` → `open_upnl` field + "open uPnL"
  column in md/html (flatten-now value; 0 for a closed book; None→"—" when
  any position lacks a mid — a partial sum is never shown). Marks stay out
  of net/edge/sharpe/DD; `hlbot track-record --paper-mark` (default on)
  fetches mids only when open paper positions exist (`--no-paper-mark`
  opt-out, fetch failure degrades to "—"). Live-fired on real allMids.
- [x] **B-PAPERLOOP — Ship the paper forward-test loop (the G1 evidence
  pipeline was DEAD).** Done (Iter 85): the live box ticks `--live` only and
  `filter_live_agents` drops unpromoted agents, so since the box went live
  (≥Jun 9) NO paper evidence accrued anywhere — the deploy DB held ONE paper
  tick total (15:42 Jun 12; breakout_v1/breakout_er_v1: zero paper rows EVER)
  and every "G1 ≈ mid-July" forward-test ETA assumed a 30d calendar clock
  that was not running. deploy/README even warned about this exact hole
  ("evidence accumulates only where paper ticks run") but nothing ran them.
  Now `hlbot-paper-tick.timer` (5min, boot-offset from the live tick) runs
  `run-paper-tick.sh`: paper `femr_tick` + `supervisor` against a dedicated
  `data/hlbot_paper.sqlite` (exported inside the script so a stray HLBOT_DB
  in /etc/hl-bot/env can never point it at the live DB; HLBOT_TICK_ARGS
  deliberately unread so the live box's `--live --execution maker` can't
  leak in; no ingest — the live account's fills stay out of the paper
  evidence stream). Wired: install.sh enables it on fresh boxes; update.sh's
  existing hlbot-*.timer self-enable loop ships it to the live box
  automatically; litestream.yml replicates the paper DB too (losing it
  resets every candidate's G1 clock). 5 safety pins in
  tests/test_deploy_paper_tick.py + exec-bit auto-pin. **All G1 calendar
  clocks start when the timer lands (Jun 12) → earliest 30d paper span ≈
  Jul 12** (B-EDGE2f, breakout_er_v1, xmom_v1).
- [x] **B-PAPERHB — Paper-loop liveness warning in `hlbot health`.** Done
  (Iter 86, the Iter-85 idle-queue item): B-PAPERLOOP's failure mode — a dead
  paper timer silently stopping every G1 calendar clock — had no detector;
  the main health check watches the LIVE DB only, and the paper loop writes
  its heartbeats to a separate file. Now `read_paper_signals(db_path)`
  (paper DB resolved beside the live DB / via HLBOT_PAPER_DB, exactly
  run-paper-tick.sh's rule; read-only open; self-reference and read failures
  degrade safely) + a warn-only `paper` check in `assess_health` (stale
  >1h ≈ 12 missed 5-min fires, or present-but-never-ticked), gated to boxes
  where a paper DB exists so dev/live-only clones stay silent. Names shared
  with run-paper-tick.sh pinned by test. 7 new tests; live-fired all three
  arms (absent → quiet, stale → warn, fresh → ok).
- [x] **B-FEEDHB — health warns when a candle feed dies under a beating
  loop.** Done (Iter 94, idle-queue find): every `enrich_view` fetch degrades
  per-coin to "skip", so a TOTAL feed outage (rate-limit, API regression)
  leaves the tick completing and the heartbeat landing while the agents on
  that feed see no bars and hold forever — for weeks indistinguishable from
  "no signal" (breakout/xmom would accrue "0 trades" instead of G1 evidence;
  the only trace was an unread per-tick stdout line). Now heartbeats carry
  per-feed coverage (`tick_heartbeats.feeds` JSON — first real schema
  migration: idempotent ALTER in `init_db`, legacy rows stay NULL),
  `empty_feeds()` flags a feed the latest tick still required that read
  0 coins across every beat in a 2h window (≥3 obs; recovery, roster-dropped
  keys, legacy rows, pre-migration DBs all stay quiet), and `assess_health`
  warns for the box's own loop (`feeds`) and for the paper loop via
  `PaperSignals.empty_feeds` (`paper_feeds`). Warn-only by design. 10 new
  tests; live-fired: pre-migration deploy DBs degrade to quiet, deploy main
  DB migrated in place under the running old-code loop (additive nullable
  column; named-column INSERTs unaffected).
- [x] **B-OPSGATE — `hlbot agent-mode`: the GO_LIVE switch as a validated,
  audited command.** Done (Iter 91): the documented procedure for the most
  consequential operation in the system — flipping an agent live — was raw
  SQL against the live DB: no agent-name validation (a typo'd INSERT creates
  a dead row while the real agent silently stays paper), no evidence
  readout, no audit trail, and NO unpause path anywhere (`_pause` sets
  enabled=0; nothing — not even the supervisor's promote upsert — ever sets
  it back; the operator's only resume was hand-edited sqlite). Now
  `supervisor/operator.py` + `hlbot agent-mode`: tightening always applies;
  loosening (rank-up or becoming live-capable) needs `--confirm`, moves ONE
  rank at a time (paper→live_small→live, mirroring `_demote`'s ladder), and
  re-checks the supervisor's own promotion evidence gates
  (`_evidence_blockers`) — flipping against failing gates needs
  `--override-evidence` on top, and the override lands verbatim on the
  `goal_evaluations` audit trail (`goal_name='operator'`, shape-proof
  against the clean-guardrails breach query). `--enable` clears pause
  markers (the missing resume). Read-only views: roster-wide state table +
  per-agent evidence readout (book span, 30d breaches, last promotion
  evaluation). GO_LIVE.md promote/halt sections now lead with the command
  (SQL kept as break-glass). 16 tests; refusal paths live-fired (exit 1,
  evidence printed). Ready for the ~Jul 12 promotion-readiness window.
- [x] **B-PAPERDB — gates/agent-mode read paper evidence from the paper DB
  (split-book coherence).** Done (Iter 95): B-PAPERLOOP moved the real paper
  book + the paper supervisor's audit trail into `data/hlbot_paper.sqlite`,
  but every paper-evidence reader still opened ONE conn (HLBOT_DB = the live
  DB) — on the live box `hlbot gates` judged an empty paper book ("no
  evidence yet" for every candidate forever) and `hlbot agent-mode`'s
  evidence re-check saw no paper book at all, so the ~Jul-12 promotion would
  either be bogusly refused (normalizing --override-evidence) or, if the
  operator pointed HLBOT_DB at the paper DB to "fix" it, the mode flip would
  land in the PAPER DB where the live tick never looks. Now: one shared
  resolver (`ops.health.resolve_paper_db_path`, the rule run-paper-tick.sh
  uses), read-only paper conn in the CLI; `evaluate_roadmap_gates(paper_conn=)`
  judges G1 from the paper book with breach history counted from BOTH audit
  trails; `evidence_readout`/`plan_mode_change(paper_conn=)` judge paper
  evidence from the paper DB while every state write stays on the live conn;
  a pause/demote breach in EITHER trail blocks a loosening flip (live
  demotion gates a paper re-promotion and vice versa). Single-DB setups
  byte-identical (paper_conn=None). 10 tests; live-fired read-only on the
  deploy DBs (G1 rows now show the real paper book; twap_mr_v1's 13 live
  breaches count against its G1; xmom refusal names the true day-0 span).
  GO_LIVE.md + deploy/README updated — run agent-mode with the DEFAULT
  HLBOT_DB.
- [x] **B-PAPERDB2 — split-DB paper evidence for `score --paper` and the
  track record's paper section.** Done (Iter 96): `build_track_record`/
  `export` take `paper_conn` — the paper section (roster, cards, daily
  series, open positions) reads the paper DB while fills/equity/live table
  stay on the main conn; paper-only-by-EITHER-book agents stay out of the
  live table (agent_state rows for paper candidates live in the live DB),
  and pre-split legacy paper rows in the live DB are NOT resurrected (the
  authoritative book is the paper DB, matching gates/agent-mode). CLI:
  `score --paper` + `track-record` wire `_paper_evidence_conn` (read-only)
  and print which paper DB supplied evidence; `hlbot supervisor` stays
  deliberately single-DB (guardrail evaluations must land beside the book
  they judged — run-paper-tick.sh runs the paper pass), now said in code +
  README. README override recipes replaced. 6 tests; live-fired read-only
  on the deploy DBs: one `track-record` now shows live fills (twap_mr_v1
  954 trades) AND the real paper books (33/33/6 legs) in one artifact.
  Single-DB boxes byte-identical (paper_conn=None).
- [x] **B-G1SPAN — Promotion gates enforce evidence SPAN + clean guardrail
  history (G1 pre-registration, day 0 of the paper books).** Done (Iter 89):
  every promotion block keyed on `window: 30d` metrics, which bound the
  *lookback*, not the sample — a paper book born Jun 12 could print
  "promotion-ready" in ~10 days if a pocket pushed its 30d card over the
  gates (the exact thin-sample shape of the "+177bps CONFIRMED" carry
  false-positive), and the fills-sourced path AUTO-APPLIES promotions.
  G1's "≥30d paper, no guardrail breach" had no structural enforcement.
  Now `Promotion.min_span_days` (evidence book span: decision log incl.
  holds for paper, fills for live; first→last row) and
  `Promotion.clean_guardrails_days` (zero pause/demote guardrail failures
  on record in the lookback; alert fails never block — they fire on any
  materially losing day by design). Metrics-pass-but-evidence-thin emits an
  audit row ("promotion blocked: evidence span 2.0d < 30d required") since
  that's the state an operator would mistake for readiness. All 9 configs
  carry `min_span_days: 30` + `clean_guardrails_days: 30`, pinned by test —
  frozen 2026-06-12, the day the paper clocks started; loosening is an
  operator decision. Defaults 0 ⇒ legacy/inline configs unchanged. 12 new
  tests; live-fired read-only on the box's paper DB (day-0 spans 0.000d,
  evaluations clean).
- [x] **B-MAKERFILL — Honest maker-fill model in the backtester.** Done
  (Iter 50): `CostModel(maker_fill="resting")` + `--maker-fill resting` on
  backtest/confirm replay the live maker lifecycle (entries rest at the
  decision bar's mid, fill only when a later bar's mid trades strictly
  through, 1800s stale cancel like `exec/maker.py`, one quote per coin,
  exits taker); fill stats (rested/filled/expired) reported. Headline: the
  optimistic maker model was carrying the whole twap_mr maker case — live
  config flips +4.2 → −4.5bps under honest fills (G0 FAIL, IS −4.7/OOS −3.6).
  Numbers in PROGRESS Iter 50; B-MAKER-LIVE re-gated on maker-rest evidence.
- [x] **B-FILL2 — Intrabar high/low fill detection.** Done (Iter 51):
  `Frame.highs/lows` (built from candle h/l; legacy caches degrade to
  close-only), resting quotes fill on wick strictly-through (equality still
  no-fill), `filled_wick` stat, and `--maker-fill resting-close` keeps the
  old close-only bound for A/B. Close-only detection was *adverse-selecting
  the fills themselves* — it missed 46% of fills (the touch-and-revert
  winners): live config maker-rest −4.5 → **+0.3bps** (96% filled, win
  57%→65%), w=240 +0.3 → **+4.7** (beats taker +2.4 on the pessimistic
  bound), 15m/52d −13.9 → −2.1. G0 at live config still FAILS at
  prefer=maker-rest (IS +0.8 / OOS −0.7 vs +3bps bar) so B-MAKER-LIVE
  stays evidence-blocked, judged by the B-G014 multi-week arms.
- [x] **B-GATES — Roadmap gate readout (G1–G3 as code).** Done (Iter 53):
  `supervisor/gates.py` operationalizes ROADMAP §4's evidence ladder — G1
  paper (≥30d *calendar span*, edge ≥+5bps, ≥150 trades, zero guardrail
  breaches in the audit trail), G2 live-small (≥30d live span, net>0 incl.
  attributed funding, maxDD<10%), G3 track record (≥60d, sharpe(all)≥1 AND
  sharpe(30d)≥0, maxDD<10%) — + read-only `hlbot gates [--agent]`. Closes
  three holes the YAML promotion gates leave: a hot 5-day book passing
  30d-*window* checks, breach *history* being invisible (only currently-
  failing guardrails blocked promotion), and G2/G3 having no code at all.
  Informational only; promotion stays human-gated. G0 stays `hlbot confirm`.
- [x] **B-GATES2 — `capital:` bases for evidence-bearing agents.** Done
  (Iter 54): twap_mr_v1 `capital: 600` ($200/trade × 3 concurrent from
  agent_overrides.json), breakout_v1/breakout_er_v1 `capital: 60` (roster
  max_total_notional) — each with a derivation comment. Pinned by
  `test_capital_bases_match_roster_book_caps`: every YAML `capital:` whose
  agent is in the roster must equal min(max_total_notional, per_trade ×
  concurrency), so a cap change that forgets the YAML fails CI. Live-fired:
  G2/G3 DD checks now evaluate (3.3% dip → G2 PASS; 11.1% dip → named
  blocker). femr_v1 deliberately left without one — all-time negative, no
  promotion path active; add (cap $20 = $20×1 concurrent) if it ever earns
  re-evaluation.
- [x] **B6/B7 — Per-agent funding attribution + Sharpe.** Done: funding split to
  the agent holding the coin at funding time (scoring includes it in net/edge);
  per-agent Sharpe from daily PnL so sharpe-gates evaluate. Tested. (Iteration 7.)
- [x] **B6old — (superseded by B6/B7/B9b)** Map `funding_payments` to the agent
  holding the position at funding time. Done by B9b: `_agent_funding_payments` now
  splits each payment by *signed held size* from the fills replay (B9), falling
  back to decision-log equal-split only when no fills exist. (REVIEW C4. Iter 11.)
- [x] **B7 — Per-agent equity curves + Sharpe/DD.** Done: per-agent Sharpe from
  daily PnL (Iter 7) + per-agent fractional `max_drawdown`/`calmar` from a
  `capital`-based synthetic equity curve (`_daily_pnl_drawdown`), threaded via
  `score_agent(capital_base=)` and a new `AgentGoals.capital` field — so
  `funding_arb_v1.yaml`'s drawdown guardrail can finally fire (it was permanently
  N/A). Configs that want fractional gates set `capital:`. (REVIEW C5. Iter 9.)
- [x] **B8 — Record actual fill price.** Done: `femr_tick` now logs the confirmed
  `res.avg_px`/`res.filled_sz` on fill so stops/TPs key off the real entry. (M1.)
- [x] **B9 — fills→positions replay.** Done: `scoring/positions.py`
  (`replay_positions` pure state machine + `rebuild_positions(conn)`) populates
  the `positions` table from fills (net_sz, size-weighted avg_entry, accumulated
  realized_pnl/fees), surviving partial fills/flips/manual interference; wired
  into `hlbot ingest` and exposed via `hlbot positions`. (REVIEW M2. Iter 10.)

- [x] **B-PREREG — Pre-registered experiment specs + `hlbot experiment`.**
  Done (Iter 64): the two headline confirms the book is waiting on (B-G014,
  B-EDGE2b) were prose + an ETA — arms re-derivable in the moment, after
  peeking at early numbers (the forking-paths bias the confirm harness exists
  to kill), and the "is the store ripe yet?" check re-derived by hand each
  iteration. Now `backtest/experiments.py` (spec load with hard-error typo
  validation; worst-coin ripeness over the full arm universe; runner that
  builds frames once per (universe, window) and feeds each arm through
  `confirm_strategy` untouched) + `hlbot experiment <spec> [--check-only|
  --force]` (exit 3 = not ripe; --force prints an explicit "peek, NOT the
  pre-registered verdict" banner) + the two frozen specs in
  `configs/experiments/`. Honesty pins in CI: b_g014's maker arms must be
  `maker_fill=resting`, no stop+w240 combo arm, b_edge2b taker-only.
  Informational output; decisions stay operator-gated.

- [x] **B-EXPREC — Persist pre-registered experiment verdicts.** Done (Iter 71):
  `hlbot experiment` printed its verdict and exited — the evidence
  B-MAKER-LIVE / B-SCALE / breakout promotion wait weeks for would have lived
  only in terminal scrollback + hand-transcribed PROGRESS prose
  (transcription is the side channel pre-registration exists to close: an arm
  dropped, a number rounded, a forced run quietly unflagged). Now every run
  writes a self-contained JSON record to `configs/experiments/results/`
  (committed beside the specs — loop.sh's `git add -A` makes persistence
  automatic): spec name + sha256 of the frozen file (a post-hoc spec edit
  changes the hash), the ripeness readout the run happened under (gaps
  included), best-effort code rev (fill-model changes flipped verdict signs,
  Iters 50/51), the forced flag, and every arm's resolved knobs + full
  confirm numbers. Forced peeks land as visibly-named `.peek` files — an
  early look leaves a permanent trace. Same-second reruns get a suffix, never
  clobber; `--no-record` opts out. Builder/writer pure + tested; CLI wiring
  pinned by tests (recorded verdict, peek flagging, opt-out, sha match,
  git-rev degrade path).
- [x] **B-POCKET — Profit time-concentration diagnostic in the confirm
  harness.** Done (Iter 76): two strategy families in two days (xmom Iter 74,
  1h breakout Iter 75) turned out to be one Apr–Jun pocket wearing a G0
  badge, and the Jun-20 b_edge2b verdict was due to be judged for exactly
  that shape — by eyeball. Now `confirm.max_window_pnl_share` (O(n) sliding-
  window min over the per-bar equity curve) reports the share of net PnL
  earned in the best 25%-of-sample contiguous window (~0.25 diffuse, ~1.0
  one pocket, >1 = rest of sample loses) per scenario — IS/OOS and every
  cost-ladder rung — with the window's UTC dates; fields ride
  `ScenarioResult` so `hlbot confirm`, `hlbot experiment`, and the persisted
  verdict records all carry it automatically (frac recorded per-field so a
  future change can't redefine old records). Informational ONLY — verdict
  logic untouched, frozen specs unmodified. First real reading (already-seen
  52.4d 15m sample, NOT a peek — b_edge2b's gate waits on post-Jun-12 data):
  the breakout-ER combined-arm G0 PASS has **taker-1x pocket 0.69
  (May 25–Jun 5), IS 0.87, OOS 2.20 (Jun 2–5: the OOS tail outside one
  4-day burst LOSES)** — the PASS is the pocket, quantified. Jun-20 baseline
  for comparison.
- [x] **B-POCKET2 — Pocket-aware prior-run comparison in `hlbot experiment`.**
  Done (Iter 80, the Iter-76/79 idle-queue "reading aid"): the rerun protocol
  says "read each verdict against history" but the machinery didn't — the
  headline table had no pocket column and prior verdicts lived in JSON records
  + PROGRESS prose. Now (a) the current-run table carries `pocket is/oos/1x`
  (1x = the cost-ladder rung matching the arm's execution basis), and (b) every
  full run prints a "Prior recorded runs" table built from the spec's persisted
  records (`load_experiment_records` + `arm_comparison`, pure + tested;
  pre-B-POCKET records degrade to "—", peeks labeled). (c) The b_edge2b
  baseline is now machine-readable: ran the frozen spec `--force` on the
  already-seen 52.5d sample (the Iter-76 numbers, honestly recorded as
  `results/b_edge2b.20260612T153311Z.peek.json`) — combined-ER pocket
  IS 0.86 / OOS 1.81 / taker-1x 0.69; original-universe full-sample pocket
  **1.15** (rest of sample loses — its PASS is wholly the pocket). The Jun-20
  verdict will print beside these automatically.
- [x] **B-RIPE — Gap-aware experiment ripeness.** Done (Iter 70): `check_ripeness`
  judged spans only — `coverage_of` computes interval-aligned holes but the
  ripeness gate discarded them, so a harvester outage >3.5d (permanent 1m data
  loss) would still let b_g014 "ripen" on span and run the pre-registered
  verdict on a corrupted sample, with the gap relegated to a dim coverage line.
  Now `CoinSpan.missing/missing_pct` + spec-level `max_missing_pct` (default
  1.0%, pinned on both frozen specs) gate ripeness alongside span; `days > 0`
  trims to the window the run would use (an out-of-window gap can't block a
  spec forever); summaries disclose gaps even when under the cap. Also probed
  + pruned: HL funding retention is ≥200d on every coin checked (BTC/XPL/ZEC/
  HYPE) — no funding store needed for 60–90d backtests.

- [x] **B-STORESYNC — Union sync between the two same-host candle stores.**
  Done (Iter 84): two independent harvesters feed two stores on this host
  (deploy clone's hourly `hlbot-harvest.timer`, ralph loop's per-iteration
  step-0) and EACH has already had a multi-day outage (the 203/EXEC-dead
  timer Jun 8–12, the unparsed loop.sh step Iter 78) — one more 3.5d+ gap in
  whichever store the B-G014 experiment reads permanently invalidates the
  multi-week 1m sample. Now `store.sync_stores` (pure, atomic, per-file
  error isolation; later-reaching side wins conflicting open times since
  only the newest stored bar can be non-final; unreadable side heals from
  the healthy one) + `hlbot harvest-candles --sync-peer DIR` (runs even on
  the if-stale skip path — sync is local and free; absent peer clone skips
  quietly so the flag is safe on other hosts; sync failures never turn the
  timer red). Wired three ways: PROMPT step 0 + loop.sh (loop→deploy) and
  hlbot-harvest.service (deploy→loop, ships via update.sh's unit-cp).
  Live-fired: stores converged to the identical union — deploy store gained
  the 12,702 bars it lacked (incl. the irreplaceable Jun 8–9 1m history),
  loop store pulled 177 fresher ones, 0 missing bars on both. Either
  harvester dying alone can no longer gap the sample; host loss remains the
  residual risk (off-host store backup = operator-gated follow-up if ever
  needed). _Host-loss coverage shipped Iter 90 (B-STOREBKP below) — arming
  it is still the operator's call._
- [x] **B-STOREBKP — Off-host S3 backup of the candle store.** Done (Iter 90):
  B-STORESYNC's residual risk is the HOST — both store clones live on it, and
  NOTHING here is replicated off-host (litestream is inactive on this box:
  no binary, no /etc/litestream.yml), so host loss permanently invalidates
  the multi-week 1m sample every P0 experiment clock is waiting on (~Jun 26
  b_g014). Litestream only covers SQLite anyway; the store is gzipped JSON.
  Now `backtest/store_backup.py`: stdlib-only SigV4 S3 PUT (no boto3/aws-cli
  on the box; signing pinned to the AWS-published key-derivation vector),
  creds from AWS_* env or the EC2 IMDSv2 instance role (live-fired: role
  resolves real temp creds), gated on `HLBOT_STORE_BACKUP_S3=bucket[/prefix]`
  (unset = inert, pinned), throttled ~hourly via a state marker beside the
  store, stable `candle_store.tar` overwrite + one dated weekly restore point
  (a corrupted store can't replace the only good copy). Wired into
  `harvest-candles` on BOTH paths (post-sync, so the tarball is the union;
  failures warn but never redden the timer — pinned). Operator: set the env
  in /etc/hl-bot/env + give the role s3:PutObject (deploy/README §Operate,
  env.example). tests/conftest.py guards the suite from real uploads on an
  armed box.
- [x] **B-STOREBKP2 — health warn when an ARMED store backup is silently
  failing.** Done (Iter 97), pulled forward from "once the operator arms it"
  so the silent-failure window never exists: arming and the watch now land
  together. `ops/health.py` `BackupSignals`/`read_backup_signals` reads the
  `.candle_backup_state.json` marker via the SAME env gate + path resolution
  `backup_store` writes with (reader/writer cannot diverge; marker written
  by the real uploader in the round-trip test). `assess_health(backup=)`:
  unarmed ⇒ no check; armed + no/corrupt marker ⇒ warn "no upload has ever
  succeeded"; armed + last success >3 h (≈3 missed hourly fires) ⇒ warn
  "going stale"; warn-only, never pages or blocks ticks. Wired into `hlbot
  health`; deploy/README backup section notes the watch.
- [x] **B-M4 — Auto-tuner auto-apply is risk-tightening only.** Done (Iter 63,
  REVIEW M4): `scripts/auto_tuner.py` was the last ungated live-params writer —
  Hermes cron auto-applied LLM tweaks to `agent_overrides.json`, including
  LOOSENING moves (its prompt rule 5 says loosen entries when "winning but
  trading rarely"; sigma_enter could drop 50%, twap notional rise to $200) with
  zero backtest evidence. Now: validated changes are partitioned by a per-key
  `RISK_DIRECTION` table — strictly-tightening changes auto-apply as before;
  loosening/ambiguous ones (exits, take-profit, anything without a current
  value) are written to `configs/agent_overrides.tuner_proposed.json` for
  human merge, mirroring `hlbot research-strategies`. The pre-M4 standing
  approval (TWAP scale-to-$200) is preserved behind explicit
  `HLBOT_TUNER_APPLY_LOOSENING=1`. Paths env-overridable → script unit-tested
  for the first time (5 tests, incl. pre-existing rails pinned).

- [x] **B-HB — Real tick heartbeat for the dead-man switch.** Done (Iter 65):
  `hlbot health`'s "is the bot alive?" check keyed on `MAX(ts_ms)` from
  `agent_decisions` — but every tick runs `log_holds=False`, so decision rows
  are event-driven (orders/errors only) and a healthy-but-quiet book read as
  DOWN after 15 trade-free minutes (false pages → muted pager → dead dead-man
  switch), while an actually-dead loop was indistinguishable from a quiet
  market. Now: `tick_heartbeats` table (one row per COMPLETED `femr_tick`,
  paper or live; auto-created by the idempotent schema) written via tested
  `runtime.record_tick_heartbeat` at the END of both tick paths (an aborted
  tick doesn't beat — that's the point); `assess_health` keys tick-freshness
  on it (crit when stale), demotes the legacy decision-based check to
  warn-only fallback for pre-heartbeat DBs, and gains an `activity` check
  (loop beating but zero decision rows for ≥3d → warn — the silent-stall
  signal the G1–G3 evidence accumulation had no detector for). Live-fired
  both directions on a real paper tick.

- [x] **B-DEPLOY-EXEC — Auto-deploy was dead on every box: update.sh shipped
  without the exec bit.** Done (Iter 79): `deploy/update.sh` was git-tracked
  100644 from birth (Jun 8) — `hlbot-update.service` ExecStart failed
  203/EXEC every 15 minutes on every checkout, so the live deployment froze
  at its install-day commit (55 commits behind: B-FUNDGR/B-FUNDGR2 funding
  guardrails, B-GR1 snapshot guardrails, B-M5 spot fix, B-HB heartbeats —
  none protecting the live book). The operator had fought this exact issue
  (8dad672 sets `core.fileMode false`) but the +x never landed in git, and
  fileMode=false makes a workdir chmod invisible to `git add -A` — which is
  also why it kept not landing. Fixed three ways: index mode → 100755 via
  `git update-index --chmod=+x`; unit hardened to `ExecStart=/usr/bin/bash
  …/update.sh` (a future lost bit can't re-kill auto-deploy; propagates via
  update.sh's own unit-cp on the first successful run); CI pin
  (`tests/test_deploy_exec_bits.py`: every deploy/*.sh + ralph/loop.sh must
  be index-mode 100755). Box remediated (`chmod +x` on the deploy clone —
  invisible to the merge under fileMode=false) and the first successful
  auto-deploy observed live same-iteration.
- [x] **B-EXITONLY — Demoted agents' live inventory gets exit-only management.**
  Done (Iter 80, live incident): at 15:07 the box's OLD pre-deploy code
  auto-promoted twap_mr_v1 paper→live_small off paper cards (the exact path
  B-PAPER3c later closed), it entered TON+NEAR in its one live window
  (~$195 each — the old tuner's $200/trade standing approval), and the edge
  guardrail demoted it the same tick (7d −10.4bps < −10). Result: **$390
  notional on a $49 account, unmanaged** — `filter_live_agents` drops a
  demoted agent entirely, and the empty-roster early return in `femr_tick`
  skipped exits, maker-fill reconciliation (TON's fill was never promoted to
  ownership — the DB owned only NEAR), stale-quote cancels, guardrails, AND
  the heartbeat (so `hlbot health` read the loop as down; no pager configured
  either). Fix: `runtime.exit_only_live_agents` (skipped agents with live-book
  ownership or working maker quotes — paper state never qualifies) re-enter
  the live tick EXIT-ONLY; `execute_decisions(exit_only=)` drops their
  entries before any other check while flattens always execute (even under a
  guardrail halt); the empty-roster early return now records a heartbeat.
  Exposure can only shrink — entries stay gated by promotion exactly as
  before. NOT a promotion path. Residual policy question (operator):
  flatten-on-demote (supervisor closes the book at demotion time) would be
  stricter than exit-ladder unwind; today's exits are reversion/stop/4h
  max-hold, which bounds the unwind to hours.
- [x] **B-DEPLOY-HB — Updater visibility in `hlbot health`.** Done (Iter 81):
  two warn-only signals (never page, never block ticks), gated on
  `HLBOT_AUTO_UPDATE=1` so non-deploy clones stay quiet. (1) *Updater
  liveness*: update.sh now touches `data/.update_heartbeat` on every
  COMPLETED run (no-op + tests-red included — those are the updater working;
  an aborted run doesn't beat), and a missing/stale (>2h ≈ 8 missed fires)
  marker warns — this is the signal both 203/EXEC incidents lacked.
  (2) *Deploy lag*: on-disk repo HEAD (read pure from .git files, no
  subprocess — update.sh ff-merges BEFORE its test gate, so HEAD advances
  even when deploy is refused) ≠ `.deployed_sha` content warns with both
  shas — catches stuck-red-tests freezes. `read_deploy_signals` anchors at
  the DB's data dir (markers live beside the DB); marker filename drift
  pinned by test against update.sh's text. The new update.sh self-deploys;
  the marker appears on the fire after that (one transient
  "never completed" warn cycle, by design).
- [x] **B-PAGER — `hlbot health` warns when no alert channel is wired.** Done
  (Iter 82, the operator nudge filed in Iter 81): the live box runs with
  `HEALTHCHECK_URL`/`TG_BOT_TOKEN`/`TG_CHAT_ID` all empty and no Hermes
  fallback, so every DOWN verdict died in the journal — perfect detection,
  zero reach. `PagerSignals`/`read_pager_signals` (env truth, mirrors the
  send paths incl. the Hermes token fallback) + a warn-only `pager` check in
  `assess_health`, gated to DBs that have actually ticked so dev/loop clones
  never nag. Telegram-only is ok-with-caveat, not a warn (it can't catch a
  fully dead box — only the missed dead-man ping does; operator's call).
  Clears when the operator sets either env in `/etc/hl-bot/env`.
- [x] **B-M5 — Spot-mid normalization fixed + tested (REVIEW M5, the last
  unpicked finding).** Done (Iter 66): the basis feed was silently dead, not
  merely fragile — the inline parser zipped `universe` with the ctx array
  positionally (live API: 305 vs 590 rows, misaligned past index ~71) AND
  wei-scaled a midPx that is already USDC-quoted; the sanity band (coded ±50%
  vs documented ±5%) rejected the garbage, so `spot: []` forever and basis_v1
  could never trade — but a payload drift landing mis-parsed mids inside ±50%
  would have meant max-size phantom paper entries (enter bar is 0.2%). Now
  `runtime.normalize_spot_mids` (pure, 5 tests): by-name ctx join, unscaled
  midPx, real ±5% band, degrade-to-{} on malformed payloads. Live-fired:
  spot mids adopted for BTC/ETH/SOL at +4–12bps basis; paper tick shows
  basis_v1 holding honestly below its 20bps entry. REVIEW is now fully swept.

- [x] **B-FUNDGR — Daily-loss guardrail counts funding (clamped) + paper rows
  can't claim live funding.** Done (Iter 68): `check_guardrails`' 24h loss
  summed `closed_pnl − fee` from fills ONLY — funding lands in
  `funding_payments`, so a book parked against extreme funding (femr's exact
  regime; at 5× notional an extreme print can rival the 3%/day limit) could
  bleed past the halt without printing a fill. Now the attributed bot funding
  (`scoring.agents_funding_since`, reusing the B6/B9b size-weighted split)
  joins the measure, clamped to ≤0: a funding loss tightens the halt, income
  never widens headroom (symmetric inclusion = loosening = operator call,
  documented in-code). Attribution failure degrades to fills-only with a
  warning (a crash here would also skip risk-REDUCING flattens). Bundled
  fix: `_coin_holders_over_time` now reads live rows only — the equal-split
  fallback could leak PAPER decision-rows into REAL funding attribution
  (scorecards too, not just the guardrail). Per-agent clamping done Iter 77
  (B-FUNDGR2 below). The duplicate user_state fetch was fixed in Iter 69
  (B-GR1 below).
- [x] **B-FUNDGR2 — Per-agent funding clamp in the daily-loss guardrail.**
  Done (Iter 77, the B-FUNDGR noted-not-done follow-up): the income clamp
  was aggregate — `min(0, Σ funding)` — so with mixed funding signs on the
  book one agent's collection masked another's bleed (+$50 carry vs −$8
  femr counted $0; the bleed never tightened the halt). Now
  `scoring.agents_funding_breakdown` (per-agent totals, deduped;
  `agents_funding_since` is its sum — one attribution path) and the
  guardrail counts `Σ min(0, per-agent funding)` — strictly tighter,
  byte-identical verdict when all agents' funding shares a sign (today's
  single-strategy live book → no live behavior change). Breach message
  shows total AND counted funding. This is the rail the B-SCALE
  multi-agent book was missing alongside B-AGG.
- [x] **B-GR1 — Guardrails judge the tick-start account snapshot.** Done
  (Iter 69, the Iter-68 found-(b) follow-up): `check_guardrails(account=)`
  consumes the `AccountState` femr_tick already fetched instead of
  re-fetching user_state + spot USDC mid-tick — the halt verdict and the
  risk caps now judge the SAME truth (previously two reads seconds apart
  could diverge within one tick), two fewer API calls per live tick, and
  one fewer mid-tick crash point ahead of the risk-reducing flattens (a
  retry-exhausted user_state fetch there aborted the whole execution
  loop). Legacy fetch path kept for snapshot-less callers; no-snapshot +
  no-Info fails SAFE (halt new entries, never fail open). +3 tests:
  Poison-Info no-fetch pin, fetched/injected verdict identity (notional
  breach via injected assetPositions + capital floor via spot+perp),
  fail-safe arm. The check→placement fill race is documented in-code as
  pre-existing and bounded by the pre-tick cap layer.
- [x] **B10 — WebSocket market view.** Done: `ingest/ws.py` MarketState +
  `hlbot ws` service writes a snapshot; live tick overlays it (HLBOT_WS_SNAPSHOT)
  for sub-second mids, L2 book_top, and a real liquidations feed (fixes C6), with
  REST fallback. (Iteration 5.)
- [x] **B10b — userFills WS for instant maker-fill detection.** Done: WS
  `userFills` channel captured into `MarketState.user_fills` + persisted in the
  snapshot; `ingest_ws_user_fills` upserts them via a shared `upsert_fill`
  (deduped by (hash,tid) against REST), and `femr_tick --execution maker` folds
  them in before `reconcile_maker_fills` so a quote that filled seconds ago is
  reconciled THIS tick, not next REST poll (fixes the C7 cadence gap for makers).
  `run_ws(user_address=)` subscribes. Unit-tested offline. (Iteration 19.)
- [x] **B-book — Book-aware maker pricing.** Done: `maker_limit_price` joins the
  near touch (best bid/ask) from the WS L2 book; live maker entries price off
  `view.book_top` (fallback mid, never cross). Tested. (Iteration 8.)
- [x] **B11 — Retire or feed liq_cascade.** Done: retired the phantom REST
  `{"type":"liquidations"}` call (always returned nothing) and added a
  `liquidations_feed` flag. liq_cascade now opens new positions ONLY when a real
  feed is present (WS trades liquidation flag / backtest), else it emits an
  explicit "feed unavailable" hold; exits still run so positions aren't stranded.
  (REVIEW C6. Iteration 12.)
- [x] **B12 — Consolidate execution paths.** `runtime.run_tick` vs `femr_tick`
  duplicate logic; unify so the safe wrapper is what live uses. (REVIEW M3.)
  **Done so far:** (a) the live order-placement loop is extracted from `femr_tick`
  into `runtime.execute_decisions` (pure of presentation, returns `ExecEvent`s)
  and unit-tested with a fake exchange (M3/D2). (b) the decision-gathering loop is
  extracted into `runtime.gather_decisions` — both `run_tick` (paper) and
  `femr_tick` (live) now share it, and `femr_tick` gained the per-agent `decide()`
  crash isolation it lacked (a broken agent no longer aborts risk-reducing
  flattens). Unit-tested. (Iteration 14.)
  (c) the inlined clearinghouse parse + per-agent reconcile loop are extracted into
  tested `runtime.positions_from_clearinghouse` + `runtime.reconcile_agents`,
  removing ~25 lines of untested CLI code. (Iteration 15.)
  (d) the allocator cap resolution loop (MetaAllocator → resolve_agent_caps →
  per-agent cfg mutation) is extracted into tested `runtime.apply_allocator_caps`
  returning `AllocatorCaps`, removing ~30 lines + 2 now-dead imports from the CLI.
  (Iteration 16.)
  (e) the WS snapshot overlay (additive merge of fresh mids/funding/book_top +
  real liquidations feed) is extracted into tested `runtime.overlay_ws_snapshot`
  returning `WsOverlay`; the CLI keeps only the env-read + file load + print.
  (Iteration 17.)
  (f) the bot-owned/manual position partition (the per-agent `bot_owned_coins`
  union + manual-coin split) is extracted into tested
  `runtime.classify_position_ownership` returning `PositionOwnership`; the CLI
  keeps only the femr `live_positions` line + display. (Iteration 18.)
  (g) the clearinghouse/spot account fetch + derived sizing values (perp
  account value, spot USDC, unified portfolio value, withdrawable) are
  extracted into tested `runtime.fetch_account_state` → `AccountState`;
  failure semantics preserved exactly (perp fetch failure aborts the tick —
  never size risk blind; spot outage degrades to $0 USDC — tightening-only).
  (Iteration 44.)
  (h) overrides loading + roster construction + the live-state filter are
  extracted into tested `runtime.load_agent_overrides` / `build_roster` /
  `filter_live_agents` (the canonical roster + auto-tuner merge now live in
  one place; `_filter_live_agents_by_state` deleted from the CLI). Hardening:
  a non-object overrides top level used to crash the tick at roster build —
  every overrides failure mode now degrades to built-in defaults with a
  warning. (Iteration 45.)
  (i) `_enrich_view` moved verbatim to `runtime.enrich_view` and the whole
  view pipeline (REST fetch → VWAP/σ + spot + 15m enrichment → opt-in WS
  overlay, window resolved CLI > env > default, 15m feed sized by the roster)
  composed into tested `runtime.build_tick_view` → `TickView`; BOTH
  `run_tick` (paper) and `femr_tick` (live) consume it, so every tick decides
  on a live-identical view. CLI keeps only printing. **Done** (Iteration 46) —
  the `femr_tick` preamble contains no untested logic. Vestigial `tick`
  command roster split out as B12j.
- [x] **B12j — Retire or unify the vestigial `tick` command.** Done (Iter 52):
  RETIRED, not unified — since B-PAPER made the paper book real, `hlbot tick`
  was a live footgun: it logged funding_arb_v1 (reference skeleton) paper
  `place` rows straight into the book that `score --paper`, the track-record
  paper section, and supervisor paper guardrails all replay. `run_tick`
  deleted (last caller); paper ticks of the full roster are `femr_tick`
  (deploy's path, unchanged). Veto stays runnable as read-only `hlbot veto`
  (same VetoAgent logic, prints verdicts, logs NOTHING; agent now
  direct-tested). funding_arb_v1 remains an importable documented skeleton.
  Found: `veto.current_vetoes` has zero callers — the verdict-row consumption
  hook never grew consumers; left in place as documented API.
- [x] **B13 — Move hardcoded trader address to config.** Done (Iter 6, M6):
  `exec/orders.py` and `scripts/daily_scorecard.py` both resolve the trader address
  from `HL_TRADER_ADDRESS`/`HL_ADDRESS` env, legacy default only as fallback.
- [x] **B14 — Go-live runbook in-repo.** `docs/GO_LIVE.md`: gated checklist,
  secrets/env, promote/kill-switch/rollback, monitoring. (REVIEW D3.)
- [x] **B10c — `hlbot ws` never subscribes userFills.** Done (Iter 56):
  `ws` command resolves the trader address (vault-aware chain via
  `exec.orders.resolve_trader_address`, promoted public) and passes
  `user_address=` to `run_ws`, so the deployed WS service finally captures
  own-fills and B10b's instant maker-fill detection is no longer dormant.
  Wiring pinned by two CLI tests (personal + vault-precedence arms);
  live-fired against the real socket — subscription accepted, snapshot
  carries `user_fills`. No deploy change needed (unit's EnvironmentFile
  already provides the env).
- [x] **B10d — `hlbot ws --seconds N` never exits.** Done (Iter 57): `run_ws`
  wraps subscribe + duration loop in try/finally and calls
  `info.disconnect_websocket()` on every exit path (duration elapsed,
  exception, Ctrl-C) — the SDK `Info(skip_ws=False)` ws thread is non-daemon
  and previously hung the process (exit 124 under timeout). Side effect:
  `run_ws` is now unit-testable with a fake Info — subscription wiring
  (allMids/userFills/per-coin l2Book/trades/activeAssetCtx) pinned by test
  for the first time. Live-fired: `--seconds 5` exits 0 in ~6s.
- [x] **B14a — Deploy automation.** Done — this entry was a stale duplicate of
  the Iter-5/6 Done item; audited (Iter 53): `deploy/` covers the whole
  description (install.sh idempotent EC2 bootstrap, test-gated auto-update.sh,
  systemd tick/report/ws/update/harvest units + timers, Litestream DB
  replication, aws/ Terraform, setup-loop.sh) with no secrets in-repo.
  Nothing left to build.

## P0.5 — prove the edge at scale (now that twap_mr_v1 is live + profitable)

- [ ] **B-SCALE — Scale twap_mr_v1 as it earns.** Let the 5×/1× risk rule +
  MetaAllocator grow size at each gate; bump caps deliberately, never to chase losses.
  This builds the track record on meaningful size.
- [x] **B-AGG — Enforce the aggregate 5× portfolio cap in code.** Done (Iter 67):
  `resolve_agent_caps` documented the two-layer rule but enforced only the
  per-agent 1× clamp — the 5× *sum* held only while the roster stayed ≤5
  agents (an accident of roster size, not a rule), and the MetaAllocator's
  cold-start/negative floors push every agent to its full 1× ceiling exactly
  when the portfolio shrinks (drawdown = when the aggregate cap must bind
  hardest). Now the resolved book scales down proportionally when Σ totals
  exceeds 5× portfolio (per-trade follows its total down, an explicit smaller
  per-trade is never raised, under-cap books come back byte-identical —
  tightening-only). 6 new tests incl. an end-to-end `apply_allocator_caps`
  pin (6 cold agents × $30 1× ceiling vs $150 5× cap → $25 each). No live
  behavior change today (live roster ≤5 agents); this is the rail B-SCALE's
  multi-agent growth was missing.
- [x] **B4-RUN — Confirm carry strategies on real history.** Done across Iters
  20–23 + 47: every confirm FAILS walk-forward (latest: 1h baseline IS
  −43.7bps, 1h beta-neutral IS −19.0bps, 1d thin-sample gated). Nothing
  promotable; carry hunt closed under B1c.
- [x] **B-UNIV — Widen the carry universe. PRUNED** (Iter 22): 10→20 coins made
  xfund WORSE (−4.3→−12.1bps maker, OOS −23.6); high-|funding| alts are
  volatile *because* funding is extreme, so more names = more price variance,
  not more edge. 50–100 coins is more of the same direction — don't re-explore
  without a new neutralization idea.
- [x] **B15c — Track-record CHART export.** Done: `reports/track_record.to_html`
  emits a self-contained HTML page with an inline SVG equity curve + per-agent
  table; `hlbot track-record` → `track_record.html`. Tested. (Strategy-review iter.)

## P0.6 — strategy experiments (validate on REAL data before flipping live)

See `docs/STRATEGY_REVIEW.md`. `twap_mr_v1` levers ship default-OFF; the loop A/Bs
each on ≥90d real history (`hlbot confirm` / `hlbot backtest --config`) before flip.

- [x] **B-REGIME — A/B `regime_filter`** on twap_mr. Done at 1h cadence (Iter 27,
  90d 10-coin, numbers in PROGRESS): clear dose-response — best config
  (`regime_min_move_pct=0.015, regime_min_consistency=0.55`) improves maker edge
  −5.0→−3.0bps, halves maxDD (−21.7%→−9.9%), cuts trades 30% — but never flips
  positive (OOS −6.0bps, G0 FAIL). Defaults (0.03/0.65) are inert at 1h. Verdict:
  the lever's direction is validated, keep default OFF, do NOT flip live until it
  A/Bs positive at live-like cadence (B-CAD; blocked on retention → B-HIST).
- [x] **B-HIST — Rolling fine-candle accumulator (prereq for B-CAD).** Done
  (Iter 28): `backtest/store.py` (merge-by-t, fresh-wins, atomic gz writes) +
  `hlbot harvest-candles` (10-coin universe × 1m/5m/15m, per-pair error
  isolation) + `hlbot-harvest.timer` (hourly, enabled by install.sh AND
  self-enabled by update.sh — auto-update previously copied new timers without
  enabling them). Validated on real API: 30/30 pairs, full retention captured
  (3.5d@1m / 17.4d@5m / 52.1d@15m, 3.6MB), incremental top-up proven.
- [x] **B-HIST2 — Backtest from the store.** Done (Iter 30): `--source store` on
  `hlbot backtest`/`confirm` via `store.frames_from_store` (candles from
  `data/candle_store/`, funding still API-fetched, seeded 2h before the first
  bar; `--days 0` = everything stored; backtest-only `--no-funding` for offline
  price-only runs) + per-coin `StoreCoverage` gap report so a harvester outage
  can't silently pass a holey sample as a full one. Validated on the real store
  (10 coins × 1m, 0 bars missing): maker +4.6bps, confirm **G0 PASS** —
  matches the Iter-29 API-sourced run on the ~half-day-shifted window.
- [ ] **B-G014 — Multi-week exact-replica G0 from the store. ← TOP PRIORITY once
  store 1m span ≥ ~14d** (harvester started 2026-06-12 → ETA ~2026-06-26; check
  spans via `hlbot harvest-candles`). Run `hlbot confirm --agent twap_mr_v1
  --coins ADA,...,ZEC --interval 1m --vwap-window 60 --days 0 --source store
  --prefer taker` **and judge any maker claim with `--prefer maker
  --maker-fill resting`** (Iter 50: the optimistic maker model is an upper
  bound that flipped sign under honest fills — a PASS that needs optimistic
  maker pricing is not evidence; Iter 51: resting is now wick-aware, a much
  tighter bound — at w=240 it already beats taker on 3.7d, so the maker-rest
  w=240 arm is the one to watch). A PASS on ≥2 weeks is the durable-edge
  evidence B-MAKER-LIVE and B-SCALE are waiting on; a FAIL means the Iter-29
  PASS was the recent pocket, not the strategy. **Run a second arm with
  `--config '{"stop_loss_pct":0.03}'`** (B-EXIT's robust lever, Iter 31)
  **and a third arm with `--vwap-window 240`** (B-WIN's 4h window, Iter 33 —
  the strongest lever so far; test arms SEPARATELY, the stop+window combo is
  anti-synergistic): whichever passes AND beats baseline on the multi-week
  sample, propose the default flip to the operator (live strategy change —
  not loop-flippable).
  **Frozen as `configs/experiments/b_g014.json` (Iter 64, B-PREREG)** — all
  six arms (baseline / stop3 / w240 × taker / maker-rest), thresholds, and
  the decision rule are pre-registered; run `uv run hlbot experiment
  configs/experiments/b_g014.json` (refuses to run until every 1m span ≥14d;
  `--check-only` is the per-iteration span readout, exit 3 = not ripe).
  _Store continuity guard: PROMPT.md step 0 (`harvest-candles
  --if-stale-minutes 30`, every iteration — Iter 78). The Iter-33 loop.sh
  top-up never ran in the long-lived loop process: bash parses the loop body
  at startup, so a loop started before the step was added (00:03 vs 02:08 on
  Jun 12) executes without it — the store survived on agents' incidental
  in-session harvests. loop.sh's step (now stale-gated too) activates when
  the operator next restarts `hlbot-loop.service`._
- [x] **B-CAD — A/B levers at live-like cadence.** Done (Iter 29, numbers in
  PROGRESS): `--vwap-window` exposed in backtest/confirm/backtest-fetch (window
  keys the cache when ≠60). Cadence explains the backtest/live divergence: maker
  edge −5.0bps (1h) → −1.5bps (5m w=12 and 15m w=4 proxies) → **+5.4bps at the
  exact live config (1m w=60, 3.5d, 844 trades, G0 PASS — sample-limited)**.
  Taker at 1m = −0.0bps: the spread tax eats the entire edge → filed B-MAKER-LIVE.
  Lever verdicts at live-like cadence: regime_filter helps at 5m (−1.5→−0.8
  maker) but is inert at 15m and unneeded at 1m; size_by_signal dampens losses
  where edge<0 but cut net profit 42% at 1m where edge>0. **Both stay default
  OFF; no live change.**
- [B] **B-MAKER-LIVE — Route live entries through maker execution.** *Was
  human-gated; now ALSO evidence-blocked (Iter 50; bracket tightened
  Iter 51).* The "+5.4bps maker vs −0.0 taker" case was built on the
  optimistic fill model (every quote fills instantly at mid). The honest
  resting replica (B-MAKERFILL) first flipped it to −4.5bps, but that was
  close-only fill detection adverse-selecting the fills themselves —
  intrabar wick detection (B-FILL2) shows close-only missed 46% of fills,
  the touch-and-revert winners. Honest pessimistic bound at the exact live
  config (1m w=60, 3.7d): maker-rest **+0.3bps vs taker −1.2** — truth ∈
  (+0.3, +4.2), both ends ≥ 0 now; at w=240 maker-rest **+4.7 beats taker
  +2.4** outright; 15m/52d −2.1 (was −13.9). Still NOT flippable: G0 at
  prefer=maker-rest FAILS at the live config (IS +0.8 / OOS −0.7 vs +3bps
  bar) and the 1m samples are one 3.7d window. Flip only if a B-G014
  multi-week maker-rest arm beats taker AND passes G0. Machinery
  (`--execution maker`, B2b/B10b) stays built and tested.
- [x] **B-SIZE — A/B `size_by_signal`.** Done as part of Iter 29 (see B-CAD):
  helps only when the strategy is losing; at the live config it cut profit 42%
  (winners get sized down too). Keep OFF. The vol-targeting variant is pruned
  unless drawdown control becomes the binding problem.
- [x] **B-EXIT — sweep `sigma_exit`/`stop_loss_pct`/`max_hold_hours`.** Done
  (Iter 31, all from cached real data, numbers in PROGRESS). One robust lever
  found: **`stop_loss_pct` 0.015→0.03 improves every live-like sample** —
  1m/3.5d maker +5.4→+6.1bps (G0 PASS, taker flips −0.0→+0.5), 5m/17d
  −1.5→−0.2 (maxDD −15.2→−12.2%), 15m/52d −1.5→−1.0; only 1h/90d disagrees
  (not the live strategy). Mechanism: a 1.5% stop at fine cadence converts
  transient wicks into realized losses before the reversion exit can pay.
  **Not flipped (live strategy change; 90d-at-live-cadence bar unmet)** —
  second confirm arm added to B-G014. `sigma_exit` tightening (0.25/0.1) is a
  fair-weather lever: big gains on the recent 3.5d pocket at every cadence but
  worse on 17d — regime, not cadence; keep 0.5 (0.0 collapses the strategy,
  proving the reversion exit is the profit engine). `max_hold_hours` inert at
  1m and 5m (reversion/stop always fires first); keep 4.
- [x] **B-FUND — funding-aware fade suppression.** Done (Iter 32, numbers in
  PROGRESS): `funding_filter` lever ships default-OFF (`funding_allows_fade`,
  hourly-rate threshold) + a units fix it surfaced — backtest `view.funding` was
  the per-bar-scaled rate (60× too small at 1m vs live); engine now feeds the
  hourly series (`Frame.funding_hourly`, legacy caches backfilled). **Verdict:
  pruned at live cadence** — at the exact live config the filter HURTS at every
  threshold (maker +5.4→+4.4..+4.6bps; the adverse-funding fades were the
  profitable ones — extreme funding against a fade marks the crowded positioning
  the reversion harvests). Helps only on 5m/17d (−1.5→−0.5, clean dose-response,
  maxDD −15.2→−10.7%) but still G0 FAIL (OOS +1.6bps); inert at 15m/1h. Same
  window flips sign across cadence (5m/3d helps, 1m/3.5d hurts) → keep OFF.
- [x] **B-WIN — VWAP window study.** Done (Iter 33, all from the candle store,
  numbers in PROGRESS). **The 4h window is the best lever found so far** —
  monotone improvement 0.5h→4h with an interior peak at 4h (8h decays) on ALL
  THREE live-like samples: 1m/3.5d maker +4.5→**+7.6** (taker flips −0.8→**+1.7**,
  G0 PASS, OOS +13.4), 5m/17.4d −1.4→**+2.1** (first positive 17d config; G0
  near-miss IS +2.0/OOS +2.4 vs +3 bar), 15m/52d −1.5→**+1.1**. Unlike
  sigma_exit/funding_filter it is concordant across samples. Caveats: fewer
  trades (991→328 at 1m) so net$ is lower ($90→$50); stop_loss 0.03 is
  ANTI-synergistic with the 4h window (both confirm arms degrade) — B-EXIT's
  stop verdict was conditional on w=60. Volume-weighted σ variant not explored
  (separate slice if ever needed). Live flip blocked on B-WIN2 plumbing +
  B-G014 multi-week evidence.
- [x] **B-WIN2 — Parameterize the live tick VWAP window.** Done (Iter 34):
  `femr_tick --vwap-window N` + `HLBOT_VWAP_WINDOW` env (CLI > env > 60;
  `runtime.resolve_vwap_window`, garbage falls through to default).
  `_enrich_view` now fetches `window`×1m candles and computes VWAP/σ via the
  backtester's `rolling_vwap_sigma`/`closes_vols` (inlined copy deleted), so
  live math == backtest math bar-for-bar; `closes` is the window slice like
  backtest frames. Default 60 — no live change. Operator flip documented in
  deploy/README.md §Going-live; gated on B-G014's multi-week evidence.
- [~] **B-EDGE2 — hunt a second, low-correlation edge** (carry is pruned) so the
  book isn't single-strategy before raising AUM. **First candidate found (Iter 35):**
  `breakout_v1` (Donchian close-channel momentum, `agents/breakout.py`) — on
  15m/52.2d store data the 96h channel posts maker +41.9 / **taker +36.4bps**
  (322 trades, Sharpe +4.9, maxDD −9.1%), **G0 PASS** (IS +26.0 / OOS +75.9,
  robust to taker-3×); 48h channel also passes (not knife-edge); ex-ZEC still
  passes (taker +18.5, OOS +68.5 — IS sharpe 0.98 marginal). Clean dose-response
  in channel length (4h −7.4 → 96h +36.4 taker). Caveats: one 52d regime sample;
  maker fills optimistic for momentum (use taker numbers); edge lives at 15m
  cadence / 48–96h horizon, NOT the live 1m loop. **Breadth caveat (Iter 48,
  B-EDGE2d): the edge does NOT generalize cross-sectionally — a 10-coin fresh
  universe FAILS G0 on the same window (OOS −31.5bps taker). The original-
  universe PASS is real but regime+universe specific; promotion bar now
  includes a breadth arm. Iter 49 (B-EDGE2e): an efficiency-ratio entry gate
  largely repairs the breadth failure and flips the combined 20-coin book to
  G0 PASS — current best candidate, forward-testing in paper as
  breakout_er_v1.** Remaining:
  - [x] **B-EDGE2a — paper wiring.** Done (Iter 38): `closes_key` config on
    breakout (default `"closes"`, backtests untouched), `closes_15m` feed in
    `_enrich_view` sized by `runtime.closes_15m_bars(agents)` (0 ⇒ zero extra
    API calls — live mode today, since the live filter drops unpromoted
    agents), roster entry with the validated lb=384/ex=96 config ($20/trade,
    $60 cap) + `configs/breakout_v1.yaml` (paper, promotion→live_small only).
    Live-fire verified: tick 1 entered XPL long `is_paper=1` (opposite twap_mr's
    XPL short!), tick 2 replayed it and held. Paper G1 evidence now accumulates
    wherever paper ticks run.
  - [ ] **B-EDGE2b — revalidate as the store grows** (15m span 52d→90d+):
    rerun the confirms each few weeks; momentum is regime-fragile. First rerun
    done (Iter 48): original universe still PASSES on today's window under the
    new `min_trades` floor (IS +20.1/226 tr, OOS +70.4/96 tr, taker). **Future
    reruns are THREE-armed** (Iter 49): original universe, breadth universe,
    and the combined 20-coin book with `min_efficiency_ratio=0.1` — the
    B-EDGE2e configuration whose G0 PASS is the current promotion candidate.
    A durable edge claim needs the ER-filtered combined arm to keep passing
    (and the threshold to stay off-knife-edge) as samples lengthen.
    **Frozen as `configs/experiments/b_edge2b.json` (Iter 64, B-PREREG)** —
    three taker arms, ripeness-gated at 60d 15m span (~Jun 20: first sample
    that outgrows the 52d window every breakout number so far shares); run
    `uv run hlbot experiment configs/experiments/b_edge2b.json`, then bump
    `min_span_days` so the next rerun waits for new data again. **Read the
    Jun-20 verdict against B-EDGE2g below: the same time-horizons at 1h on
    208d FAIL all three arms (IS strongly negative); a 15m PASS whose gain
    still lives entirely in the Apr–Jun pocket is the warned-about shape.**
    The pocket question is now a number (B-POCKET, Iter 76): the verdict
    record will carry `pocket_share` per scenario; today's already-seen-
    sample baseline for the combined-ER arm is taker-1x 0.69 / IS 0.87 /
    OOS 2.20 — a PASS whose pocket numbers don't fall on new data is the
    pocket renewing its badge, not a durable edge. _Baseline now recorded
    (Iter 80, B-POCKET2): `results/b_edge2b.20260612T153311Z.peek.json`
    (combined-ER pockets 0.86/1.81/0.69 on the one-day-later window); the
    Jun-20 run prints its numbers beside it automatically._
  - [x] **B-EDGE2g — extended-history read at 1h cadence (span-for-cadence).**
    Done (Iter 75): the Iter-74 1h harvest made ~208d available NOW vs
    ~Sep for 90d of 15m, so the xmom-killer test was pointed at breakout
    months early. Frozen `configs/experiments/b_edge2_1h.json` (same TIME
    horizons rescaled to native 1h bars: lb=96/ex=24/er_lb=24 = 96h/24h/24h;
    stop/hold/cooldown already in hours; three taker arms mirroring
    b_edge2b) and ran the pre-registered verdict same-day on the ripe store:
    **all three arms FAIL** — IS-on-extended-history (mostly pre-April,
    never touched by any breakout run) is strongly NEGATIVE (original
    −10.5bps/994tr, breadth −17.6/1024tr, combined-ER −13.9/1294tr taker)
    while the +12.6..+17.1 OOS tails sit inside the already-seen Apr–Jun
    selection window; full-sample taker negative on all arms (−2.6/−6.9/
    −4.0bps). Same shape that killed xmom (Iter 74). Frozen caveat: 1h
    evaluation is a DIFFERENT experiment (4× coarser exits) — this cannot
    fail breakout_er_v1's 15m case (b_edge2b stays the gate) and the OOS
    cadence cost is real (+16.3 at 1h vs +36.1 at 15m on overlapping
    windows), but it is a strong regime-fragility warning: across two
    strategy families (xmom, Donchian) and two cadences, momentum's profit
    on this tape is one Apr–Jun pocket. Record:
    `results/b_edge2_1h.20260612T125649Z.json`; min_span_days bumped
    150→200 post-run (LIT binding → next rerun ~Jul 10, beside b_edge3).
  - [x] **B-EDGE2d — out-of-universe breadth test.** Done (Iter 48): same
    config (lb=384/ex=96) on 10 fresh liquid coins (CRV,ENA,LIT,NEAR,SUI,TON,
    WLD,XMR,XPL,XRP — top fresh by 24h volume, full 52d history) **FAILS G0**:
    full-sample taker +9.8bps but walk-forward IS +37.4 / OOS **−31.5bps**
    (172 trades, Sharpe −2.91). Same calendar OOS window where the original
    universe earns +70.4. Per-coin attribution (single-coin runs): original
    OOS gain is BROAD majors-trend (ADA +154, SOL +118, ETH +112, BTC +100);
    original IS was carried by ZEC (+156) + HYPE (+55); fresh OOS bleed is
    broad mid-cap chop (NEAR −110, WLD −97, LIT −83, ENA −83). The 52d "edge"
    = two regime pockets, not a universal cross-sectional property. breakout_v1
    stays paper-only; numbers in PROGRESS. Breadth universe now harvested at
    15m (`harvest extra_pairs`, `--breadth-coins`) so re-tests outgrow the
    rolling ~52d API retention.
  - [x] **B-EDGE2e — trend-quality (efficiency-ratio) entry gate.** Done
    (Iter 49): `min_efficiency_ratio`/`er_lookback_bars` on breakout (default
    OFF), Kaufman ER over 24h of 15m bars at entry. ER ≥ 0.1 removes the
    near-zero-ER false breaks the breadth FAIL was made of: breadth OOS
    −31.5→−1.6bps (still FAIL), original universe PASS strengthens
    (+36.4→+39.2 taker, OOS +70.4→+88.6), and the **combined 20-coin book
    flips FAIL→G0 PASS** (taker +43.9bps, OOS +36.1/sharpe +3.96/156 tr,
    robust to taker-3×; effective band 0.1–0.2, not a knife-edge; <0.05
    inert). Threshold chosen on the breadth sweep → same-window selection;
    forward test = `breakout_er_v1` paper A/B arm (roster + config, beside
    unfiltered breakout_v1). NOT promotable until B-EDGE2b's three-armed
    reruns confirm on longer samples. Numbers in PROGRESS.
  - [ ] **B-EDGE2f — ER-arm correlation + paper A/B readout.** When the paper
    books have ≥30d: `hlbot correlate` breakout_er_v1-config vs twap_mr_v1
    (expect ≈ breakout's −0.1) and compare the two breakout paper cards —
    the filtered arm should show fewer trades and better edge if the
    backtest result is real out-of-window. _Clock correction (Iter 85,
    B-PAPERLOOP): no paper book existed before Jun 12 — the box never ran
    paper ticks. The ≥30d mark is ~Jul 12, counted from the paper timer's
    first fire, and the book lives in the box's `data/hlbot_paper.sqlite`._
  - [x] **B-EDGE2c — quantify correlation to twap_mr_v1.** Done (Iter 36):
    `backtest/correlate.py` (UTC-day PnL bucketing + Pearson, tested) +
    `hlbot correlate` (two arms, per-arm config/vwap-window, same frames/cost
    model). On the 15m/52.2d 10-coin store sample, breakout_v1 (96h channel)
    vs twap_mr_v1 daily-PnL corr is **−0.08 taker / −0.16 maker** (w=4 live
    proxy) and **−0.07 / −0.10** (w=16 4h-window candidate) over 54 days —
    uncorrelated (n=54 → ±0.27 CI95; claim is "uncorrelated", not "hedge").
    Diversification thesis holds; rerun alongside B-EDGE2b as the store grows.

- [~] **B-EDGE3 — third edge candidate: cross-sectional momentum (`xmom_v1`).
  Promotion case DEAD on extended history (Iter 74)** — the first
  pre-registered b_edge3 run (208d × 1h, 2.3× the selection sample) FAILS
  all three arms: combined IS **−11.8bps**/470tr, original-10 IS −13.1,
  breadth IS −27.4; the +51/+45 OOS tails are exactly the already-seen
  momentum pocket the 14d lookback was selected on. Iter 72's +67bps
  full-sample edge was the pocket, not the strategy (full-sample taker on
  208d: +4.2bps ≈ nothing). Paper agent stays (zero-risk out-of-time
  forward test, the cleanest arbiter if the regime returns); promotion bar
  unchanged and this record now stands in front of it. Original finding
  (Iter 72): `agents/xmom.py` — rank coins by trailing return, long top-K /
  short bottom-K dollar-neutral, rank-hysteresis exits + stop/max-hold; on
  90d × 1h × 20 coins the 14d lookback passed G0 at taker (IS +19.5, OOS
  +131.7, full +67.0, robust to taker-3×) with same-window-selection
  caveats — which the extended sample has now confirmed were fatal.
  Daily-PnL corr vs breakout-ER: −0.01 (uncorrelated). Numbers in PROGRESS
  Iters 72/74.
  - [x] **B-EDGE3a — paper wiring.** Done (Iter 73): `closes_1h` feed in
    `enrich_view` (sized by `runtime.closes_1h_bars` — shared
    `_closes_feed_bars` helper with the 15m feed; covers `skip_bars`; 0 ⇒
    zero extra API calls in live mode since the live filter drops unpromoted
    agents), `TickView.bars_1h` + CLI summary line, roster entry with the
    G0-passing config (lb=336, top_k=2, $20/leg × 4 = $80 book) +
    `configs/xmom_v1.yaml` (paper, capital 80, promotion→live_small only,
    gates ≈ half the backtest edge). Live-fire verified: tick entered the
    full dollar-neutral book (WLD+LIT long / SOL+ZEC short, ranks 1/2/15/16
    of 16), next tick replayed and held it. G1 paper evidence accumulates
    wherever paper ticks run (~30d sample ≈ mid-July).
  - [x] **B-EDGE3b — pre-registered rerun spec.** Done (Iter 74): decided
    1h-harvest over 15m-rescaling (native bars = the validated config;
    rescaling changes exit/stop timing). `harvest-candles` now collects 1h
    for both universes (DEFAULT_INTERVALS + BREADTH_INTERVALS; loop.sh and
    the systemd timer pick it up via defaults, CLI-default drift pinned by
    test) — first harvest captured ~208d (LIT 171.9d worst). Froze
    `b_edge3.json` (three taker arms, lb336/skip0, vwap_window 337) and ran
    the first pre-registered verdict same-day on the immediately-ripe
    extended sample: **all arms FAIL** (record:
    `results/b_edge3.20260612T124331Z.json`; see umbrella above).
    min_span_days bumped 150→200 post-run per the frozen protocol — next
    rerun ~Jul 10 with ~28d of post-selection accrual.

- [x] **B-REV — momentum-reversal (fade) screen. PRUNED without a backtest
  (Iter 83)** — pure arithmetic on the existing b_edge3 + b_edge2_1h records.
  A sign-flipped momentum book pays the same round-trip cost C the momentum
  book paid, so `reversal_net = −mom_net − 2C` (engine C: taker 13bps RT,
  optimistic-maker 2bps RT). Condition to clear zero at taker:
  `mom_net < −26bps` somewhere. Across EVERY recorded segment (3 xmom arms +
  3 Donchian arms × IS/OOS/full on 208d×1h, plus the Iter-72 90d lookback
  sweep at 72/168/336 bars), exactly one cell clears: xmom-breadth IS −27.4
  → fade +1.4bps — below the +3bps G0 bar, IS-segment-only on the most
  expensive-to-trade universe, with the same-calendar OOS at −30.4. The
  optimistic-maker screen has one positive (xmom-breadth full −9.2 → +5.2)
  sitting on the fill model Iter 50 proved flips sign under honest resting
  fills — and fade entries (quote against the prevailing move) are exactly
  the adverse-selection shape the resting model punishes. Verdict: this tape's
  momentum is too weak to ride at taker and not negative enough to fade —
  the gross alpha lives inside the ±26bps cost band almost everywhere.
  Numbers in PROGRESS Iter 83. **Standing cheap re-check (no new task):**
  each pre-registered b_edge3 / b_edge2_1h rerun (~Jul 10) prints fresh
  IS/OOS/full nets — re-read this screen off the new record; only if an arm
  prints net < −26bps taker on IS *and* full-sample coherently does fading
  earn an actual backtest.

## P3 — capital formation (see docs/CAPITAL.md)

- [x] **B15 — Public-grade track-record export.** Done: `reports/track_record.py`
  + `hlbot track-record` → track_record.{json,md}. Chart export = B15c above.
- [x] **B16 — Hyperliquid vault evaluation.** Researched (docs/CAPITAL.md): ~10%
  profit share, ≥5% leader TVL, ~1d depositor lockup, API-wallet-compatible. **Verify
  creation fee in current HL docs before launch.** Gate behind G3 track record.
- [x] **B16b — Vault launch checklist + bot retargeting.** Done (Iter 55):
  `HL_VAULT_ADDRESS` env retargets the WHOLE bot at a vault — orders signed
  with `vaultAddress` (build_exchange; account_address alone only redirects
  reads) AND every account read (Settings.hl_address → ingest/equity,
  HL_TRADER_ADDRESS → guardrails/account fetch/open orders, daily_scorecard)
  follows the vault; malformed value refuses to run rather than fall back to
  the personal account. `hlbot doctor` grew a `vault` check; launch checklist
  in GO_LIVE.md §Vault retargeting (flatten-first, watched first fill,
  rollback). Found+fixed: CAPITAL.md step 5 ("point HL_TRADER_ADDRESS at the
  vault") would have read the vault but traded the personal account. Still
  human-gated behind G3; env unset ⇒ behavior byte-identical.
- [x] **B-PROP — Prop/funded eval prep.** Done (Iter 58): `docs/PROP_EVAL.md`
  checklist (verify-terms table, free pre-screen, rule→guardrail mapping with
  the realized-vs-equity buffer, isolation wiring, in-eval/abort discipline) +
  the operational core: `risk/prop.py` `EvalProfile`/`simulate_eval` replays
  any equity curve against eval rules our guardrails do NOT model
  (equity-based day-boundary daily loss incl. unrealized, trailing-HWM /
  static max drawdown, profit target + min trading days) and `hlbot
  prop-check` runs it read-only on `equity_snapshots`. Eval *run* stays
  human-gated behind live G1+ evidence per the checklist. The actual eval
  needs ≥30d clean `prop-check` on the live box first.
- [x] **B-PROP2 — Pre-screen backtest equity curves through `EvalProfile`.**
  Done (Iter 59): `hlbot backtest --prop-profile '{json}'` screens each
  run's per-bar equity curve through `simulate_eval` (rules JSON →
  `parse_eval_profile`, hard-error on typos like `--config`; start balance
  = `--starting-capital` so rules and curve share a base; trading days from
  the engine's simulated fills; per-exec-mode one-line verdicts via
  `EvalReport.summary()`). Informational only — a FAIL prints, never gates.
  Live-fired on real 1m store data: taker arm fails a 1%-daily eval on an
  intraday dip (987.17 vs floor 987.47) invisible to coarser sampling.
  PROP_EVAL.md Step 1 documents the screen.
- [x] **B17 — Moonshot sleeve spec.** Done (Iter 60): `docs/MOONSHOT.md` —
  ring-fence invariants (hard cap = one written-down tranche ≤1–2% of
  capital, isolated-margin-only [HL's defined-max-loss primitive], per-bet
  margin ≤25% of cap, ≤2 concurrent, kill floor 25% → DEAD + ≥90d stand-down,
  sweep-to-core ratchet, address ∉ {trader, vault}), bet discipline
  (pre-registered thesis/invalidation/max-loss, no averaging down, funding
  in the budget), refund rules (fresh decision, ≤1 tranche/quarter, never
  death-week, two dead tranches = re-evaluate the concept), measurement
  (excluded from the public track record by construction — own address, DB
  never ingests it). Rules-as-code: `risk/sleeve.py` `SleeveConfig`/
  `evaluate_sleeve` + read-only `hlbot sleeve-check` (live-fired on the real
  API). Funding stays operator-only, junior to live G1+ evidence; the bot
  never trades the sleeve.

## Done

- [x] **B-GATE — Two measurement-integrity fixes** (Iter 47, found via B1c):
  (a) coarse bars (4h/1d) now SUM the actual hourly funding settlements inside
  the bar instead of extrapolating the last sampled rate ×4/×24 (which paid an
  extreme print for a whole bar — flattering exactly the carry strategies
  coarse backtests test); ≤1h paths byte-identical. (b) `hlbot confirm` gained
  a `--min-trades` per-split floor (default 20): a +bps edge on 2 trades can
  no longer print "✅ CONFIRMED" (a real 1d carry run did exactly that).
  Tightening-only; prior G0 PASSes (844-trade twap_mr 1m, 322-trade breakout)
  clear the floor.
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
- [x] **B-PERF — linear-time `build_frames`.** Per-frame prefix scans (bars-so-far,
  funding sweep, 1440-bar vol sum) made fine-interval backtests quadratic; replaced
  with per-coin cursors + volume prefix-sums, equivalence-tested against the old
  logic verbatim. 90d×5m×2-coin build: 0.16s. (Iteration 27.)
