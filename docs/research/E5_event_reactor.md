# Infra Spec: E5 — event-driven liquidation reactor

> Priority: after S4/S5 research, before liq_cascade leaves paper. From
> docs/ALPHA_ROADMAP.md §2/§3. This is the book's only latency-sensitive
> edge; all speed investment concentrates here.

## Problem
liq_cascade_v1's edge (post-cascade dislocation reversion) decays in seconds.
The engine evaluates agents on a polling cycle — by the time a cycle sees the
cascade in `view.extra["liquidations"]`, the bottom is often in. The WS
process (`hlbot ws`) already receives liquidation events in real time and
logs them to data/liq_log.jsonl; it just can't act.

## Design (speed of reaction, never relaxation of gates)
1. `hlbot ws` gains a trigger: when rolling cascade notional over T seconds
   ≥ `min_liq_notional_usd` (calibrated from liq_log by S2) for a tracked
   coin, write `data/liq_trigger.json` {coin, notional, ts_ms} (atomic
   rename), debounced to ≥1 per coin per cooldown.
2. The engine loop selects on a short poll of that file (or SIGUSR1) between
   cycles; on trigger it runs a **single-agent mini-cycle** for liq_cascade
   only: fresh WS snapshot view → decide → the NORMAL execution path.
   Same kill check, guardrails, cooldown, order-rate, sizing caps — the
   mini-cycle is faster, not looser. Target: < 1s from WS event to order.
3. Telemetry: log trigger→order latency per event into exec-quality so S2
   calibration can regress capture vs. latency.

## Non-goals
No separate fast executor, no bypassing the audited path, no WS process
placing orders (it stays key-less). If <1s through the normal path proves
impossible, measure where the time goes before changing the design.

## Validation
- Unit: trigger debounce, atomic handoff, mini-cycle uses full gate stack
  (a test that asserts kill/cooldown/caps are consulted on the trigger path).
- Calibration: S2 replay over liq_log answers whether sub-second entry beats
  cycle-latency entry by enough to matter (if not, E5 is unnecessary — let
  the data kill it).

## Acceptance
liq_cascade paper trades show trigger-driven entries with logged latency;
zero orders outside the gate stack; CI green.
