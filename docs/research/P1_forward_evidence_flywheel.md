# P1 — Forward-evidence flywheel (the binding constraint)

> Status: **spec.** V3 (params_hash provenance), the trust prerequisite, is
> **landed** (see "V3 — landed" below). The accrual schema + nightly
> auto-confirm loop are the build items this spec authorizes.

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

- Build the confirm frames from the **forward-accrued** window (paper-soak
  candles + `market_samples`), not just back-fetched history, so the OOS split
  grows night over night.
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

## Build order (follow-ups this spec authorizes)

1. **P1a-1** migration + `market_samples` accrual in the engine cycle (cheapest,
   unblocks OI/imbalance forward study and true S8).
2. **P1c** nightly `confirm --record` wrapper + systemd timer (the auto-confirm
   loop; (d) already consumes it).
3. **P1b** full-universe paper roster for the unconfirmed agents (couples to P2
   universe expansion + `build_frames` perf).
4. **P1a-2** `xvenue_funding` (host) and `listing_log` accrual (S5/S7 fuel).

Each follow-up: spec-bounded, additive, gate-neutral. Record dead ends honestly.
