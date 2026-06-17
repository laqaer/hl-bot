# Alpha Roadmap — Forward-Evidence Flywheel + V3 params_hash

> **Status:** P1 spec approved; implementation in progress.  
> **Mission:** Drive hl-bot toward a compounding $1M book by maximizing the number of G0-confirmed, diversified +EV edges running automatically, with strict risk control. Optimize for breadth of confirmed edges and forward-accrued evidence — NOT raw compute or latency.

---

## 1. What is true now

- Only the mean-reversion core (`femr_v1`, `twap_mr_v1`, `twap_mr_regime_v1`) is live in paper/small-live; carry is economically trivial on Hyperliquid (<1%/yr) and dead-ended as a standalone edge.
- The agents referenced as `dislocation_reversion_v1`, `funding_crowding_fade`, and `new_listing_reversion` do **not exist** in the current branch. The existing event/reversion sleeve is `liq_cascade_v1` (data-fed by WS) plus the MR agents. This roadmap builds the flywheel for the *existing* roster first and leaves clearly-marked extension points for D2a/D2b/S8 when they are implemented.
- Backtesting is exhausted as a discovery engine: HL retention caps history (5m ≈ 17.5d, 1h ≈ 190d), so low-frequency or recently-invented edges cannot reach the G0 floor from fetched history alone.
- `hlbot confirm` currently runs agents with `config={}` — i.e. Python constructor defaults — **not** the deployed `configs/agent_overrides.json`. This means a passing confirmation may not match what is actually trading.
- `confirm_strategy()` does not enforce the documented G0 trade-count floor (≥30 IS / ≥10 OOS trades) and does not persist results.
- Durable signals (OI, per-new-listing first-seen, cross-venue funding, L2 book imbalance) are available in memory every tick but are not written to SQLite.

**Conclusion:** the binding constraint is forward evidence, not compute or latency. The next edges must be confirmed forward.

---

## 2. P1 — Forward-evidence flywheel

### 2a. Append-only signal accrual

New SQLite tables (idempotent; no deletes):

| Table | What | Populated by | Frequency |
|---|---|---|---|
| `market_snapshots` | per-coin mid, 1h funding, OI, 24h volume, book top | `fetch_market_view()` + WS snapshot | every tick (5m) |
| `new_listings` | first-seen ms, first listed px, initial candles | `detect_new_listings()` | every tick |
| `funding_cross_venue` | HL/Binance/Bybit 1h funding rate | `ingest_cross_venue_funding()` | hourly (nightly MVP) |

Principles:
- Upsert-only. We never rewrite history.
- Writers are called from existing paths (`femr_tick`, WS service) so no new long-running daemon is required for the MVP.
- `market_snapshots` becomes the forward window for `confirm-forward`.

### 2b. Continuous paper roster

The existing 5-minute tick always runs the full roster in paper mode, even when live mode is active:
- `femr_v1`
- `twap_mr_v1`
- `twap_mr_regime_v1`
- `liq_cascade_v1`
- `basis_v1`
- (future) `funding_crowding_fade_v1` — stub: logged as "registered but not built"
- (future) `new_listing_reversion_v1` — stub: logged as "registered but not built"

Paper decisions are logged to `agent_decisions` with `is_paper=1` and the agent's current `params_hash`. They never reach live order placement.

### 2c. Nightly auto-confirm loop

New CLI: `hlbot confirm-forward [--window-days 30] [--min-is-trades 30] [--min-oos-trades 10]`

For each paper agent:
1. Load the deployed config (`defaults + configs/agent_overrides.json`) and compute `params_hash`.
2. Rebuild the forward window of `Frame`s from `market_snapshots` since the agent's `last_confirmed_ms`.
3. Run `confirm_strategy()` on that window under the preferred execution model.
4. Persist the full result to `confirmation_results`.
5. If PASS:
   - verify the passing `params_hash` still matches the deployed config;
   - verify supervisor guardrails are not breached;
   - if `mode == 'paper'`, promote to `live_small` and record `confirmed_params_hash`, `confirmed_at_ms`, `last_confirmed_ms`.

Scheduled via `deploy/systemd/hlbot-confirm-forward.timer` at 00:05 UTC daily.

**Acceptance:** an agent crossing G0 on forward data promotes with no human step beyond the monthly capital decision.

### 2d. V3 params_hash — prerequisite for trustworthy auto-promotion

Every agent gets a deterministic content hash of its effective config:
- `normalize_config()` recursively sorts keys, rounds floats, coerces ints.
- `hash_config()` returns a 16-char truncated SHA-256 of the normalized JSON.
- The hash is computed at agent instantiation and stored in:
  - `agent_configs` registry table,
  - `agent_decisions.params_hash`,
  - `fills.params_hash`,
  - `confirmation_results.params_hash`,
  - `agent_state.confirmed_params_hash`.

Backtest, confirm, and live instantiation all use the same factory path so confirmation validates the **deployed** config, not constructor defaults.

---

## 3. G0 gate (unchanged thresholds, hardened checks)

A strategy is G0-confirmed only when:
- in-sample net edge ≥ +3 bps,
- out-of-sample net edge ≥ +3 bps,
- out-of-sample Sharpe ≥ 1.0,
- **≥30 IS trades and ≥10 OOS trades** (new hard floor),
- cost ladder is reported; 2x taker slippage survival is reported but not required.

The gate is never weakened. If an agent fails on sample size, it soaks longer in paper.

---

## 4. Promotion ladder

| From | To | Who moves it | Condition |
|---|---|---|---|
| `paper` | `live_small` | `hlbot confirm-forward` | G0 PASS on forward data + matching `params_hash` + no guardrail breach |
| `live_small` | `live` | human operator only | G2 evidence per `docs/GO_LIVE.md` |
| any | `paper` / paused | supervisor | guardrail breach or demotion |

Full `live` remains human-gated. Auto-promotion never bypasses that.

---

## 5. Guardrails

- G0 thresholds are evidence-backed only; never weakened.
- `configs/agent_overrides.json` is the single runtime override source; the auto-tuner may only propose risk-reducing changes.
- No funded keys, `agent_state` manual edits, or unsandboxed withdrawals are performed by code.
- All schema changes are backward-compatible idempotent additions.
- Paper agents never place live orders.

---

## 6. Dead-end log

| Edge / idea | Verdict | Date | Notes |
|---|---|---|---|
| Carry on HL | dead | 2026-06 | <1%/yr after costs; kept as filter, not standalone agent |

---

## 7. Acceptance criteria

- [ ] This roadmap doc exists at `docs/ALPHA_ROADMAP.md`.
- [ ] `hash_config()` is stable and tested.
- [ ] DB schema includes `agent_configs`, `market_snapshots`, `new_listings`, `funding_cross_venue`, `confirmation_results`, and `params_hash` columns on `fills`/`agent_decisions`/`agent_state`.
- [ ] `hlbot confirm` prints `params_hash` and uses the deployed config.
- [ ] `hlbot confirm-forward` exists, persists results, and only promotes on G0 PASS + matching `params_hash`.
- [ ] Every tick writes `market_snapshots`; new listings are detected; cross-venue funding is accrued.
- [ ] Full test suite and lint pass.
