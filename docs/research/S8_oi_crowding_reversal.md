# S8 — OI-spike crowding reversal

> Status: **BUILT, soaking forward (2026-06-15).** Rostered `paper` with a
> params-matched `require_g0` ladder; confirmable ONLY forward (OI is not in
> candle history), so it accrues OOS on calendar time until it clears G0.

## Thesis

The confirmed dislocation edge works because forced/emotional flow overshoots and
reverts. `funding_crowding_fade` (D2a) sharpened it with a *crowding* gate —
fade an overshoot only when funding is extreme (longs/shorts paying up to hold).
But funding is only a **proxy** for the thing that actually marks crowding:
**open interest building fast**. S8 uses the real signal.

When OI grows rapidly (a burst of new positions over ~30min) **and** price has
gapped away from its 5m VWAP, the move is crowded and tends to mean-revert as the
late crowd gets squeezed/unwinds. Fade it:

- OI spiked (`oi_change >= oi_spike_min`) **and** `z = (mid-vwap)/sigma >= +z_enter` → **SHORT**
- OI spiked **and** `z <= -z_enter` → **LONG**

An OI spike is **unsigned** (it doesn't say long or short), so unlike
funding_crowding_fade the *direction comes from the overshoot*, and the OI spike
supplies the selectivity (the gate that makes a milder |z|~1 overshoot tradeable).

## Why it's forward-only confirmable (the P1 tie-in)

OI is served only by `metaAndAssetCtxs` (a current snapshot) — it is **never in
`candleSnapshot`**. So S8 cannot be back-tested on HL history at all; it can only
be confirmed on **forward-accrued** data. That is exactly what the P1 flywheel
provides:

1. `market_samples.open_interest` accrues OI every cycle (P1a).
2. `ingest.accrual.build_oi_change_view` computes per-coin `oi_change` =
   fractional OI growth over the lookback, from `market_samples`, each cycle, and
   writes `view.extra['oi_change']`.
3. `accrue_frame_samples` persists `oi_change` per bar into `frame_samples`
   (migration 8), alongside the vwap/sigma/mid the other agents use.
4. `load_accrued_frames` replays it into `Frame.oi_change`; the backtester exposes
   it as `view.extra['oi_change']`, so `confirm`/`autoconfirm` evaluate S8 on the
   forward window. Back-fetched bars carry no `oi_change`, so S8 simply finds no
   crowding there — only accrued bars drive its confirm, and that sample grows.

## Parameters (`OICrowdingReversalConfig`)

| param | default | meaning |
|-------|---------|---------|
| `oi_spike_min` | **0.01** | min fractional OI growth over the lookback to count as crowding |
| `z_enter` | **2.0** | min \|vol-normalized overshoot\| to fade |
| `z_exit` | 0.5 | take profit when z reverts within this of VWAP |
| `stop_pct` | 0.02 | hard stop bounds the "the crowd was right" tail |
| `max_hold_bars` | 12 | ~60min at 5m |
| `min_daily_volume_usd` | 10M | liquidity floor (so a wide universe never forces illiquid coins) |

> **Defaults are CALIBRATED, not guessed** (2026-06-16, via `s8-oi-backtest`).
> The first cut shipped `oi_spike_min=0.10` — but on ~18d of real Binance OI the
> 30-min ΔOI distribution is p90 **0.71%**, p95 **1.15%**, max **6.5%**, so a 10%
> gate **never fires** (S8 would trade zero forever and the soak accrue nothing).
> Recalibrated to `0.01` (≈p95, a genuine spike). The grid also showed a real
> ~2σ overshoot (`z_enter=2.0`) is where the OOS edge is positive (+11 to +23 bps
> vs mixed/negative at z=1.0), so `z_enter` moved 1.0 → 2.0. These are distribution-
> justified PRIORS, deliberately NOT the max-OOS-edge cell (the ~5d OOS overfits);
> the forward soak + autoconfirm is the real arbiter.

The OI lookback is `build_oi_change_view(lookback_s=1800)` (~30min), independent
of the agent so it can be tuned in the accrual layer.

Perp-only, TAKER entry (a reversion must get in now); same family as
dislocation / funding_crowding_fade.

## Promotion

`configs/oi_crowding_reversal_v1.yaml`: `roster: live`, `mode: paper`,
`require_g0` ladder meeting `tests/test_gate_minima.py` minima exactly (≥5d in
paper, ≥20 paper round trips, ≥3 bps edge, params-matched G0). It cannot reach
live until the supervisor sees a fresh, params-matched forward G0 — no gate
weakened.

## Determining the edge before the forward soak completes

HL OI isn't back-fetchable and forward OOS takes weeks, so to get an EARLY read
on whether S8 is real, use **Binance OI history as a cross-venue proxy** for
crowding (the same phase-1-offline-study posture as S5 xvenue funding):

```
hlbot s8-oi-backtest --coins BTC,ETH,SOL,DOGE,AVAX,LINK,SUI,WLD --days 30          # verdict at defaults
hlbot s8-oi-backtest --coins ... --sweep                                            # calibrate + grid
```

It loads HL 5m candle frames, overlays Binance OI-change
(`research/oi_history.py` + `backtest.data.overlay_oi_change`), and runs the SAME
G0 gate as `confirm`. OI comes from Binance's **public dumps**
(`data.binance.vision`, daily 5m `metrics` zips), which are NOT geo-blocked — so
this runs anywhere, including US hosts and CI where the fapi API returns HTTP
451. (The parsers are pure + unit-tested; only the download needs network.) A
PASS is evidence to keep soaking S8 forward, NOT a promotion: live still requires
a params-matched **forward** G0 on HL data, since Binance OI is only a proxy for
HL crowding (different participants, different microstructure).

**First run (2026-06-16, ~18d, 8 coins):** at the recalibrated defaults the edge
is **promising but not yet confirmable** — the OOS window (~5d) is too thin (the
exact sample-size problem the forward flywheel exists to solve). The sweep at
`z_enter=2.0` is consistently positive (OOS +11 to +23 bps with 10–19 trades),
which is why the defaults moved to `oi_spike_min=0.01 / z_enter=2.0`. Verdict:
keep soaking S8 forward; let `autoconfirm` settle PASS/FAIL on HL data.

## Open / next

- **Tuning (P2):** sweep `oi_spike_min` / `z_enter` / lookback — first via
  `s8-oi-backtest` on Binance OI (host), then on forward HL data as it accrues;
  the defaults are a sensible prior, not a fit.
- **Breadth compounds it:** more coins in the enrich universe (#25/#26) ⇒ more OI
  spikes observed ⇒ faster G0. The OI-change signal currently rides the same
  coins the enrich candle universe covers (they need both candles and OI).
