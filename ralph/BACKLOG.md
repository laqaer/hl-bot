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
- [~] **B2 — Maker (post-only) execution.** Primitive done: `place_limit_order`
  (post-only 'Alo'), `round_price_to`, `has_resting_order` + tests. **Remaining
  (B2b):** async resting-order fill reconciliation across ticks, then route live
  *entries* through maker (exits stay taker). Until then live entries are still
  taker. Backtest passive strategies as maker once B1 unblocks.
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
- [~] **B4-RUN — Confirm carry strategies on real history.** Run on a 10-coin
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

## P2 — cadence, structure, devops

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
  _Store continuity now guarded by loop.sh per-iteration top-up (Iter 33: the
  systemd harvest timer was found NOT running on the loop box — no root; a
  >3.5d gap would have silently invalidated this sample)._
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
    backtest result is real out-of-window.
  - [x] **B-EDGE2c — quantify correlation to twap_mr_v1.** Done (Iter 36):
    `backtest/correlate.py` (UTC-day PnL bucketing + Pearson, tested) +
    `hlbot correlate` (two arms, per-arm config/vwap-window, same frames/cost
    model). On the 15m/52.2d 10-coin store sample, breakout_v1 (96h channel)
    vs twap_mr_v1 daily-PnL corr is **−0.08 taker / −0.16 maker** (w=4 live
    proxy) and **−0.07 / −0.10** (w=16 4h-window candidate) over 54 days —
    uncorrelated (n=54 → ±0.27 CI95; claim is "uncorrelated", not "hedge").
    Diversification thesis holds; rerun alongside B-EDGE2b as the store grows.

## P3 — capital formation (see docs/CAPITAL.md)

- [x] **B15 — Public-grade track-record export.** Done: `reports/track_record.py`
  + `hlbot track-record` → track_record.{json,md}. Chart export = B15c above.
- [x] **B16 — Hyperliquid vault evaluation.** Researched (docs/CAPITAL.md): ~10%
  profit share, ≥5% leader TVL, ~1d depositor lockup, API-wallet-compatible. **Verify
  creation fee in current HL docs before launch.** Gate behind G3 track record.
- [ ] **B16b — Vault launch checklist + bot retargeting.** Steps to point
  `HL_TRADER_ADDRESS` at a vault sub-account once G3 is met.
- [ ] **B-PROP — Prop/funded eval prep.** Checklist for Hypernova/Propr/Velotrade
  (HL-native, API-friendly); run the same guardrailed strategy through an eval for
  additive $100–200k. (docs/CAPITAL.md Track B.)
- [ ] **B17 — Moonshot sleeve spec.** Design the ring-fenced, loss-bounded sleeve
  (separate sub-account, hard cap, defined max loss). Spec only; no live. (CAPITAL.md
  Track D.)

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
