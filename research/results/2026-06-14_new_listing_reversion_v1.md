# Sweep: new_listing_reversion_v1 — 2026-06-14

- dataset: 210d of 1h candles, prefer=taker
- gate: OOS edge ≥ 3.0 bps, sharpe ≥ 1.0, 2x-slippage robust
- combos: 12 (ranked by IN-SAMPLE edge; OOS columns are a one-shot readout, never the selection key)

| # | verdict | OOS edge (bps) | OOS sharpe | OOS net | trades | universe | params |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | ❌ | -497.6 | -2.56 | -4.71 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.2, "stop_pct": 0.08}` |
| 2 | ❌ | -960.1 | -3.17 | -6.36 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.2, "stop_pct": 0.12}` |
| 3 | ❌ | -77.7 | -0.39 | -0.94 | 4 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.2, "stop_pct": 0.08}` |
| 4 | ❌ | -960.1 | -3.17 | -6.36 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.2, "stop_pct": 0.12}` |
| 5 | ❌ | +82.3 | +0.33 | +0.73 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.3, "stop_pct": 0.08}` |
| 6 | ❌ | -936.2 | -4.47 | -9.28 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.3, "stop_pct": 0.12}` |
| 7 | ❌ | -482.5 | -1.76 | -3.04 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.3, "stop_pct": 0.08}` |
| 8 | ❌ | -977.2 | -3.83 | -6.49 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.3, "stop_pct": 0.12}` |
| 9 | ❌ | -482.5 | -1.76 | -3.04 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.4, "stop_pct": 0.08}` |
| 10 | ❌ | -977.2 | -3.83 | -6.49 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.4, "stop_pct": 0.12}` |
| 11 | ❌ | +82.3 | +0.33 | +0.73 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.4, "stop_pct": 0.08}` |
| 12 | ❌ | -936.2 | -4.47 | -9.28 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.4, "stop_pct": 0.12}` |

**0/12 combos confirmed.**

No combo cleared the gate — do not loosen the gate; improve the strategy or the execution model.
