# P1 — Forward-evidence flywheel (the binding constraint)

> Status: **LANDED (2026-06-15).** All four parts ship: V3 provenance (the trust
> prerequisite), the append-only accrual schema + per-cycle capture (a), the
> full-universe paper soak of the unconfirmed agents (b), and the nightly
> forward auto-confirm loop (c). The flywheel now turns automatically; what
> remains is calendar time (samples accruing) and the cross-venue/host legs
> noted under "Follow-ups still open".

## Why this is the #1 priority

Backtesting is exhausted as a discovery engine on Hyperliquid. HL's
`candleSnapshot` retains only ~5000 candles/interval (5m ≈ 17.5d, 1h ≈ 190d),
so low-frequency or recent edges **cannot reach the G0 floor** (≥30 IS / ≥10 OOS
trades) from back-fetched history. Two real, correctly-built edges already died
on this — not on direction, on **sample size**:

- `funding_crowding_fade_v1` — strong in-sample (+15bps, 3× slip robust), but
  0/36 sweep combos clear G0 on the thin ~5d 5m holdout.
- `new_listing_reversion_v1` — thesis points the right way intraday (+198bps,
  1h, 3× slip robust), but only ~9 listings → 6 trades exist in retained history.

The fix is structural: **confirm the next edges FORWARD.** Accrue the signals
that can't be back-fetched, run every built-but-unconfirmed agent in paper over
the full universe continuously, and let calendar time grow the OOS sample until
`hlbot confirm` clears G0 — then auto-promote, without a human step beyond the
monthly capital decision and without weakening any gate.

## The four moving parts

```
 (a) accrue  ──►  append-only signal tables grow every engine cycle
 (b) soak    ──►  all unconfirmed agents trade PAPER over the full universe
 (c) confirm ──►  nightly `hlbot confirm` over the FORWARD window, --record
 (d) promote ──►  supervisor auto-promotes paper→live_small on a fresh, params-
                  matched G0 (V3) — already wired, already gated, untouched
```

(d) already exists and is correct (`supervisor/goals.py::evaluate`,
`promotion_ladder` with `require_g0`). V3 makes its G0 check trustworthy. (a),
(b), (c) are the build.

---

## V3 — landed (params_hash provenance)

The hole this closed: `hlbot confirm` instantiated agents with `config={}`
(defaults), so it never validated `agent_overrides.json`; the `confirmations`
table had no params fingerprint; and `require_g0` accepted *any* fresh
confirmed row. A tuned override could therefore inherit a G0 stamp earned for a
different config — auto-promotion on forward evidence would be untrustworthy.

What shipped:

- `Agent.params_fingerprint()` / `Agent.params_hash()` +
  `agents.base.compute_params_hash()` — a stable 12-hex canonical-JSON hash of
  the agent's **resolved** params (the `cfg` dataclass = factory defaults +
  overrides merged at construction). Behaviourally-identical configs hash the
  same; any behaviour param change moves the hash.
- Migration 5: `confirmations.params_hash` column (+ index).
- `hlbot confirm` now builds the agent from the **deployed** config
  (`AGENT_FACTORIES` + `agent_overrides.json`, `--no-use-overrides` / `--params
  '{json}'` to override), prints the hash, and stamps it on `--record`.
- `g0_confirmed(..., params_hash=...)` requires the stamp to match the deployed
  config; legacy NULL-hash rows never satisfy a specific hash. `supervise()`
  computes the deployed roster's hashes (`deployed_params_hashes`) and threads
  them through `run_once → evaluate`, so the live gate is strict; the bare
  helper stays back-compatible (no hash ⇒ age-only) for tests/ad-hoc use.

Net: **confirm validates what is deployed, and promotion only fires on a G0
earned for those exact params.** No gate weakened — the gate got an extra,
evidence-backed condition.

---

## (a) Forward-accrual schema (append-only)

One job: persist, every engine cycle, the signals candle history will never
give us. Append-only, idempotent on `(ts_ms, …)` PRIMARY KEY, no deletes.
Written from the cycle's already-fetched `MarketView` / WS snapshot (zero extra
network for the HL legs).

Proposed migration (a single new migration entry; do NOT edit shipped ones):

```sql
-- forward-accrued market samples: OI + book imbalance can't be back-fetched.
CREATE TABLE market_samples (
    ts_ms          INTEGER NOT NULL,
    coin           TEXT    NOT NULL,
    mid            REAL,
    funding        REAL,          -- HL 1h funding (signed, per-hour)
    open_interest  REAL,          -- metaAndAssetCtxs openInterest (S8 enabler)
    day_ntl_vlm    REAL,
    book_imb       REAL,          -- (bidSz-askSz)/(bidSz+askSz) top-of-book, WS
    PRIMARY KEY (ts_ms, coin)
);
CREATE INDEX idx_market_samples_coin ON market_samples(coin, ts_ms);

-- cross-venue funding (Binance/Bybit free, no auth) — S5 phase 1, host-only
-- (CI sandbox is geo-blocked from those venues).
CREATE TABLE xvenue_funding (
    ts_ms        INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    venue        TEXT    NOT NULL,   -- 'binance' / 'bybit' / 'hl'
    funding_apr  REAL,
    PRIMARY KEY (ts_ms, coin, venue)
);

-- first-seen + listing price per new perp (forward listing log) — the only way
-- new_listing_reversion ever reaches a confirmable OOS sample.
CREATE TABLE listing_log (
    coin           TEXT PRIMARY KEY,
    first_seen_ms  INTEGER NOT NULL,
    listing_px     REAL,
    source         TEXT            -- 'ws' / 'meta' / 'rest'
);
```

Accrual points (no new services; ride existing loops):

| Table | Written by | Source | Cadence |
|-------|------------|--------|---------|
| `market_samples` | `hlbot run` engine cycle | MarketView (OI/funding/vlm) + WS book_top (imbalance) | every cycle (~5s–5m) |
| `xvenue_funding` | nightly host job | `research/funding_xvenue.py` (Binance/Bybit REST) | hourly/nightly |
| `listing_log` | engine cycle + WS | existing `new_listings` detector / `metaAndAssetCtxs` universe diff | on first sighting |

Sampling discipline: dedupe on the PK; downsample `market_samples` to ≤1 row
per coper per minute to bound growth (~180 coins × 1/min ≈ 260k rows/day → WAL
is fine; revisit at a year). Litestream already replicates the DB.

Cost: bounded, append-only, no edge logic. This is plumbing — it is the
fuel tank, not the engine.

## (b) Continuous paper soak over the full universe

- Every built-but-unconfirmed agent (`funding_crowding_fade_v1`,
  `new_listing_reversion_v1`, future S8) stays `roster: paper` and trades the
  **full liquid perp universe** in the paper simulator each cycle, so OOS trades
  accrue on calendar time, not on retained candles. (Universe expansion from ~8
  to the full liquid set is P2 and compounds directly here — more coins ⇒ more
  forward dislocations/events ⇒ faster G0 clearance.)
- The paper simulator + per-agent attribution + scorecards already exist; this
  is a roster/universe wiring task, not new measurement.

## (c) Nightly auto-confirm over the forward window

Host nightly (after the sweep job), for every unconfirmed roster agent:

```
hlbot confirm --agent <name> --interval 5m --days <forward-span> \
    --prefer taker --record          # uses deployed config + stamps params_hash
```

- `--refresh` (default on for `autoconfirm`) re-fetches the latest HL candles so
  the rolling window ADVANCES each night instead of re-confirming a stale cached
  dataset.
- LINCHPIN (landed): `confirm`/`autoconfirm` build frames from
  **back-fetched ∪ forward frame store** (`frame_samples`, migration 7;
  `accrue_frame_samples` each cycle; `load_accrued_frames`/`merge_frames`). HL's
  official candles win inside its ~17.5d retention; accrued bars (the rolling
  vwap/sigma/mid/funding the engine already computes, floored to the bar) extend
  the window backward, so the 5m G0 OOS GROWS forward past retention over
  calendar time instead of just rolling. (`--no-accrued` to disable.) Verified
  end-to-end: 30d soak → merged confirm window 30d vs HL's capped 17.5d.
- `--record` stamps `confirmed` + `params_hash` of the deployed config (V3).
- When an agent's forward OOS finally clears G0 with matching params, the
  supervisor's next `require_g0` check passes and promotes paper→live_small —
  **no human step.** A param retune re-arms the gate automatically (new hash ⇒
  needs a fresh confirm), which is the point.

Sequencing on the host timer: `ws` (accrue) → `run` (soak + sample) →
`sweep` (param surface) → `confirm --record` (forward G0) → `supervisor`
(promote). All already have systemd units except the confirm-loop wrapper.

## Acceptance

An agent crossing G0 on **forward-accrued** data promotes paper→live_small with
no human action beyond the monthly capital decision, and:

1. the promoting G0 row's `params_hash` equals the deployed config's hash (V3);
2. no gate, cap, or kill threshold was weakened to make it pass;
3. the forward window — not back-fetched history — supplied the OOS trades that
   cleared the ≥10-OOS floor.

## What landed (2026-06-15)

- **P1a — accrual.** Migration 6 (`market_samples`, `xvenue_funding`,
  `listing_log`). `ingest/accrual.py` writes per-cycle from the MarketView/WS
  snapshot: OI + funding + vlm + **top-of-book imbalance** (new WS capture in
  `ingest/ws.py::MarketState.book_imb`), throttled per coin; and the
  `listing_log` with a **first-run backfill guard** so the pre-existing universe
  is never mistaken for day-1 listings. Hooked into `run_cycle` before decide.
- **P1b — paper soak.** New YAML contracts put both unconfirmed agents on the
  roster: `funding_crowding_fade_v1` (roster:live, mode:paper, require_g0 ladder
  — auto-promotes when forward G0 clears) and `new_listing_reversion_v1`
  (roster:paper moonshot soak). The live `new_listings` signal
  (`build_new_listings_view`) finally lets the new-listing agent trade in paper.
- **P1c — auto-confirm loop.** `hlbot autoconfirm` re-runs the G0 gate over the
  forward window for every paper agent awaiting G0 (interval derived per agent,
  HL-retention-aware default window), `--record` stamping the params_hash.
  `deploy/run-confirm.sh` + `hlbot-confirm.{service,timer}` (03:00 UTC, after
  the 02:00 sweep). The supervisor's existing `require_g0` check (d) consumes it.

## Follow-ups still open

0. **frame-store-backed confirm — LANDED** (the linchpin for retention-capped
   agents; was Codex review #1 on PR #23). `frame_samples` (migration 7) accrues
   the per-bar signal each cycle; `confirm`/`autoconfirm` replay
   `back-fetched ∪ accrued`, so the 5m G0 OOS window grows past HL's ~17.5d over
   calendar time. Tests: `tests/test_frame_store.py`. Remaining nuance: the store
   only covers coins with a live candle signal (top-vol via `enrich_view`);
   widening that universe is P2.
1. **xvenue accrual on the host.** `accrue_xvenue_funding` exists + is tested;
   wire `research.funding_xvenue.fetch_xvenue_funding` into the nightly host job
   (Binance/Bybit are geo-blocked from CI, so this leg can't run in-sandbox).
2. **Full-universe breadth (P2).** Paper soak currently rides `enrich_view`'s
   top-20-by-volume candle universe; widening to the full liquid perp set needs
   the `build_frames`/enrich perf work (P2) to stay cheap.
3. **OI/imbalance as filters (P4/S8).** Once `market_samples` has a forward
   window, test OI-spike + book-imbalance as filters on the dislocation core,
   then a true S8 crowding-reversal agent.

Each follow-up: spec-bounded, additive, gate-neutral. Record dead ends honestly.
