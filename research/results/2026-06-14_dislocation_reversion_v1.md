# Sweep: dislocation_reversion_v1 — 2026-06-14

- dataset: 90d of 5m candles, prefer=taker — ACTUAL coverage ~17.5d (HL retains ≤~5000 candles/interval; the evidence window is this, not 90d)
- gate: OOS edge ≥ 3.0 bps, sharpe ≥ 1.0, 2x-slippage robust
- combos: 36 (ranked by IN-SAMPLE edge; OOS columns are a one-shot readout, never the selection key)

| # | verdict | OOS edge (bps) | OOS sharpe | OOS net | trades | universe | params |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | ❌ | +13.8 | +9.34 | +0.21 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 4.0}` |
| 2 | ❌ | +13.8 | +9.34 | +0.21 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 4.0}` |
| 3 | ❌ | +15.9 | +10.52 | +0.24 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 4.0}` |
| 4 | ❌ | +15.9 | +10.52 | +0.24 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 4.0}` |
| 5 | ❌ | +11.6 | +8.75 | +0.17 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 4.0}` |
| 6 | ❌ | +11.6 | +8.75 | +0.17 | 3 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 4.0}` |
| 7 | ❌ | -8.7 | -4.13 | -0.26 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 4.0}` |
| 8 | ❌ | -3.6 | -1.88 | -0.11 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 4.0}` |
| 9 | ❌ | -10.4 | -5.02 | -0.31 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 4.0}` |
| 10 | ❌ | -1.9 | -0.96 | -0.06 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 4.0}` |
| 11 | ❌ | -2.7 | -1.43 | -0.08 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 4.0}` |
| 12 | ❌ | -9.4 | -4.70 | -0.28 | 6 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 4.0}` |
| 13 | ❌ | +2.6 | +1.68 | +0.38 | 29 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 3.0}` |
| 14 | ❌ | +0.1 | +0.09 | +0.02 | 30 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 3.0}` |
| 15 | ❌ | -12.0 | -11.97 | -1.91 | 32 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 3.0}` |
| 16 | ❌ | -1.8 | -1.58 | -0.34 | 38 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 3.0}` |
| 17 | ✅ | +3.0 | +2.41 | +0.56 | 37 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 3.0}` |
| 18 | ❌ | -11.6 | -12.88 | -1.92 | 33 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 3.0}` |
| 19 | ❌ | -5.6 | -4.57 | -0.88 | 31 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 3.0}` |
| 20 | ❌ | -3.8 | -3.32 | -0.59 | 31 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 3.0}` |
| 21 | ❌ | -4.3 | -4.02 | -0.88 | 41 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 3.0}` |
| 22 | ❌ | -13.2 | -15.24 | -2.63 | 40 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 3.0}` |
| 23 | ❌ | -4.2 | -4.20 | -0.87 | 41 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 3.0}` |
| 24 | ❌ | +6.6 | +7.34 | +2.22 | 67 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 2.5}` |
| 25 | ❌ | +2.8 | +3.68 | +1.17 | 84 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 2.5}` |
| 26 | ❌ | -11.6 | -14.02 | -2.48 | 43 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 3.0}` |
| 27 | ❌ | +1.0 | +1.46 | +0.47 | 90 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 2.5}` |
| 28 | ❌ | -0.4 | -0.74 | -0.17 | 85 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 2.5}` |
| 29 | ❌ | +4.3 | +5.47 | +1.58 | 73 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 24, "stop_pct": 0.01, "z_enter": 2.5}` |
| 30 | ❌ | +1.4 | +1.78 | +0.48 | 70 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.5}` |
| 31 | ❌ | -1.0 | -1.77 | -0.39 | 81 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.5}` |
| 32 | ❌ | -2.3 | -3.60 | -1.07 | 94 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 2.5}` |
| 33 | ❌ | -1.5 | -2.23 | -0.67 | 89 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.5}` |
| 34 | ❌ | +1.9 | +2.74 | +0.71 | 75 | BTC,ETH,SOL,HYPE | `{"max_hold_bars": 12, "stop_pct": 0.01, "z_enter": 2.5}` |
| 35 | ❌ | -3.0 | -6.02 | -1.51 | 101 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.5}` |
| 36 | ❌ | -2.9 | -6.02 | -1.56 | 106 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"max_hold_bars": 6, "stop_pct": 0.01, "z_enter": 2.5}` |

**1/36 combos confirmed.**

Next actions:
- Best combo `{"max_hold_bars": 24, "stop_pct": 0.02, "z_enter": 3.0}` on `BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE` → consider promoting into configs/agent_overrides.json and stamping `hlbot confirm --agent dislocation_reversion_v1 --prefer taker --record`.
