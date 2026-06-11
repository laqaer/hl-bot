# Strategy review (2026-06-09)

A full review of the live strategy now that the dust has settled: **`twap_mr_v1`
is the one proven engine** (+29.5 bps, ~7.9 daily-Sharpe, +$159 over 556 live
trades), **the carry strategies were tested on real history and pruned** (no edge),
and the rest of the roster is paper/dormant. This documents *why* twap_mr works, its
weaknesses, the negative results, and a prioritized, testable improvement program.

## 1. The proven engine: `twap_mr_v1`

**What it does** (`agents/twap_mr.py`): each tick, for liquid coins (>$10M/24h),
compute z = (mid − VWAP₁ₕ)/σ₁ₕ from 60×1m candles. If |z| > 2, **fade** it (short
above VWAP, long below), ranked by extremity; exit on reversion (|z|<0.5), a 1.5%
stop, or 4h max-hold. A **per-coin loss veto** stops re-entering coins whose own
recent fills bled (the ADA/HYPE/AVAX churn loop).

**Why it has edge (hypothesis):** short-horizon order-flow imbalance on liquid perps
mean-reverts; a 2σ 1h dislocation is usually transient microstructure (a sweep, a
liquidation) that snaps back. The edge is **execution-sensitive** — it only survives
as a *maker* (we now post-only, earning the spread instead of paying it). The loss
veto + maker execution are doing real work; the +29.5 bps is *after* costs.

**Confidence:** real, but **thin and unproven at scale** — 556 trades over ~1 week on
one account in one regime. Sharpe 7.9 (daily) is high and likely flattered by the
short window. Treat it as a promising seed, not a finished edge. The track record
(60–90d, `hlbot track-record`) is what turns it into something fundable.

## 2. Structural weaknesses (and what we did about them)

| Weakness | Risk | Status |
|---|---|---|
| **Fades into trends** — a 2σ move that's a real breakout gets faded → stop-out, repeat. The #1 structural loss for any fader (REVIEW C3). | High | **New lever `regime_filter`** (opt-in): drops fades leaning against a strong local trend (uses `research/candidates.regime_allows_fade`). Default OFF; A/B on real data before flipping. |
| **Flat sizing** — a 2.0σ and a 4.0σ signal get identical capital, so weak fades over-consume risk. | Med | **New lever `size_by_signal`** (opt-in): scales notional with signal strength, weak fades get ~half. Default OFF; A/B. |
| **Eager exit** (`sigma_exit=0.5`) may close before full reversion; **tight stop** (1.5%) may get swept on noise. | Med | Hypothesis — tune via the loop's `--config` sweep on real data. |
| **No funding awareness** — fading a coin whose funding strongly opposes the reversion adds carry drag. | Low–Med | Hypothesis B-FUND below. |
| **Short VWAP window** (1h) — noisy σ; a 2–4h or volume-weighted window may give cleaner signals. | Low–Med | Hypothesis B-WIN below. |

The two levers ship **default-off** on purpose: the live edge is proven, so we never
change it blind — we add the experiment, validate it on real history (`hlbot confirm`
/ the config sweep), and only then flip the config.

## 3. Negative results (valuable — they prune the search)

- **Carry is dead on this account.** `xfund_carry_v1` (cross-sectional funding
  carry), `funding_carry_v1` (single-name carry), and a "hold-while-eligible" churn
  reducer were all run through the confirmation gate on **real** Hyperliquid funding
  history and **failed** — no durable net-of-cost edge (loop iters 20–23). Funding
  capture at our size/cadence is eaten by costs and adverse selection. **Do not
  resurrect these without a fundamentally different execution model** (e.g. true
  maker market-making, much larger size, or cross-venue).
- **`liq_cascade`** was fed a phantom REST endpoint (never traded); now gated on the
  real WS trades feed but unproven.
- **`femr_v1`** bleeds small (−48 bps) — keep paper.

## 4. The multi-agent system

The chassis is sound: cloid attribution, exchange-reconciled PnL, the supervisor's
promote/demote gates, the 5×/1× risk + MetaAllocator scaling, per-agent funding
attribution + Sharpe + drawdown (so guardrails actually fire), and the confirmation
harness that just earned its keep by killing carry. **The right posture now: run the
ONE thing that works (`twap_mr_v1`), keep everything else paper, and let the loop hunt
for the *next* edge while this one builds a track record.** Don't run unproven agents
live next to the proven one (they net positions and muddy attribution).

## 5. Prioritized improvement program (for the loop to validate on real data)

Each is a hypothesis; the loop confirms with `hlbot confirm` / `hlbot backtest
--config` on real history before any live flip. Capital scales only on confirmed edge.

1. **B-REGIME — A/B `regime_filter`** on twap_mr over ≥90d real history. If it lifts
   net-of-cost edge / cuts drawdown, flip it on. (Lever shipped.)
2. **B-SIZE — A/B `size_by_signal`** (and a vol-targeting variant). (Lever shipped.)
3. **B-EXIT — sweep `sigma_exit` / `stop_loss_pct` / `max_hold_hours`** for the
   best risk-adjusted exit. (Use the loop's `--config` sweep.)
4. **B-FUND — funding-aware fade suppression:** skip/trim fades where funding
   strongly opposes the expected reversion.
5. **B-WIN — VWAP window study:** 1h vs 2–4h vs volume-weighted σ.
6. **B-UNIV — universe study:** is the edge concentrated in a few coins? Positive-
   select coins with proven realized edge, not just "liquid + signal".
7. **Keep hunting a *second*, low-correlation edge** (carry is out) so the book isn't
   single-strategy — important before raising AUM.

## 6. Shipped this review

- **`regime_filter` + `size_by_signal`** opt-in levers on `twap_mr_v1`
  (`agents/twap_mr.py`) with tests (`tests/test_twap_levers.py`) — experiments #1–2.
- **Track-record HTML/SVG chart** (`reports/track_record.py::to_html`, `hlbot
  track-record` → `track_record.html`) — the shareable artifact for vault/SMA
  due-diligence (B15c).

## 7. Risk review (unchanged, healthy)

Daily-loss guardrail (~3% of equity), capital floor halt (~$40), supervisor
demote-on-drawdown (now able to fire per-agent), maker-only entries, position
reconciliation, cloid attribution. The single biggest risk to the *portfolio* is not
the bot — it's discretionary hand-trading on the same account (the −$8.5k bleed). Keep
the bot isolated and let the proven edge compound.
