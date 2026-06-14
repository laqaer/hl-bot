# Sweep: new_listing_reversion_v1 — 2026-06-14

- dataset: 210d of 1h candles, prefer=taker
- gate: OOS edge ≥ 3.0 bps, sharpe ≥ 1.0, 2x-slippage robust
- combos: 12 (ranked by IN-SAMPLE edge; OOS columns are a one-shot readout, never the selection key)

| # | verdict | OOS edge (bps) | OOS sharpe | OOS net | trades | universe | params |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | ❌ | -492.8 | -2.53 | -4.66 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.2, "stop_pct": 0.08}` |
| 2 | ❌ | -951.8 | -3.15 | -6.31 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.2, "stop_pct": 0.12}` |
| 3 | ❌ | -71.6 | -0.36 | -0.86 | 4 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.2, "stop_pct": 0.08}` |
| 4 | ❌ | -951.8 | -3.15 | -6.31 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.2, "stop_pct": 0.12}` |
| 5 | ❌ | +91.3 | +0.36 | +0.81 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.3, "stop_pct": 0.08}` |
| 6 | ❌ | -929.2 | -4.45 | -9.21 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.3, "stop_pct": 0.12}` |
| 7 | ❌ | -474.2 | -1.73 | -2.99 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.3, "stop_pct": 0.08}` |
| 8 | ❌ | -978.1 | -3.84 | -6.50 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.3, "stop_pct": 0.12}` |
| 9 | ❌ | -474.2 | -1.73 | -2.99 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.4, "stop_pct": 0.08}` |
| 10 | ❌ | -978.1 | -3.84 | -6.50 | 2 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 12, "min_runup": 0.4, "stop_pct": 0.12}` |
| 11 | ❌ | +91.3 | +0.36 | +0.81 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.4, "stop_pct": 0.08}` |
| 12 | ❌ | -929.2 | -4.45 | -9.21 | 3 | BTC,ETH,CHIP,AZTEC,SKR,LIT,FOGO,AXS,DASH,STABLE,XMR | `{"max_age_bars": 24, "min_runup": 0.4, "stop_pct": 0.12}` |

**0/12 combos confirmed.**

No combo cleared the gate — do not loosen the gate; improve the strategy or the execution model.
