# Sweep: funding_crowding_fade_v1 — 2026-06-14

- dataset: 90d of 5m candles, prefer=taker — ACTUAL coverage (HL retains ≤~5000 candles/interval; the evidence window is the SHORTEST span, not 90d): BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE ~17.5d; BTC,ETH,SOL,HYPE ~17.5d
- gate: OOS edge ≥ 3.0 bps, sharpe ≥ 1.0, 2x-slippage robust
- combos: 36 (ranked by IN-SAMPLE edge; OOS columns are a one-shot readout, never the selection key)

| # | verdict | OOS edge (bps) | OOS sharpe | OOS net | trades | universe | params |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | ❌ | +5.7 | +1.74 | +0.03 | 1 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 2 | ❌ | -13.2 | -6.11 | -0.66 | 10 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 3 | ❌ | -5.7 | -3.54 | -0.43 | 15 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 4 | ❌ | -6.2 | -2.42 | -0.22 | 7 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 5 | ❌ | +5.7 | +1.74 | +0.03 | 1 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 6 | ❌ | -2.6 | -1.16 | -0.11 | 8 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 7 | ❌ | -9.5 | -5.18 | -0.57 | 12 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 8 | ❌ | +5.7 | +1.74 | +0.03 | 1 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 9 | ❌ | -2.5 | -1.77 | -0.22 | 18 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.5}` |
| 10 | ❌ | -13.3 | -8.69 | -1.00 | 15 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 11 | ❌ | +5.7 | +1.74 | +0.03 | 1 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 12 | ❌ | +0.3 | +0.16 | +0.02 | 11 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 13 | ❌ | +2.1 | +1.25 | +0.13 | 12 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 14 | ❌ | -17.3 | -9.23 | -1.04 | 12 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 15 | ❌ | -3.2 | -2.69 | -0.36 | 22 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 16 | ❌ | +12.3 | +3.65 | +0.06 | 1 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 17 | ❌ | +0.0 | +0.01 | +0.00 | 27 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 1.0}` |
| 18 | ❌ | -13.7 | -13.92 | -1.44 | 21 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 19 | ❌ | +3.2 | +2.00 | +0.03 | 2 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 20 | ❌ | +3.2 | +2.00 | +0.03 | 2 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 21 | ❌ | +12.3 | +3.65 | +0.06 | 1 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 12, "stop_pct": 0.02, "z_enter": 2.0}` |
| 22 | ❌ | -16.0 | -14.32 | -1.45 | 18 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 23 | ❌ | -3.6 | -4.62 | -0.57 | 32 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 24 | ❌ | -12.1 | -9.99 | -0.91 | 15 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 25 | ❌ | -2.7 | -3.51 | -0.50 | 37 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.0}` |
| 26 | ❌ | +3.2 | +2.00 | +0.03 | 2 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 27 | ❌ | -5.5 | -6.19 | -0.64 | 23 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 28 | ❌ | -5.2 | -3.06 | -0.23 | 9 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |
| 29 | ❌ | -2.4 | -1.55 | -0.12 | 10 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |
| 30 | ❌ | -9.9 | -9.17 | -0.85 | 17 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 20, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 31 | ❌ | +3.2 | +2.00 | +0.03 | 2 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 32 | ❌ | -3.6 | -4.28 | -0.47 | 26 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 1.5}` |
| 33 | ❌ | -0.0 | -0.03 | -0.00 | 15 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |
| 34 | ❌ | -1.6 | -1.36 | -0.12 | 14 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 15, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |
| 35 | ❌ | -5.0 | -2.18 | -0.02 | 1 | BTC,ETH,SOL,HYPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |
| 36 | ❌ | -5.0 | -2.18 | -0.02 | 1 | BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE | `{"funding_min_apr": 30, "max_hold_bars": 6, "stop_pct": 0.02, "z_enter": 2.0}` |

**0/36 combos confirmed.**

No combo cleared the gate — do not loosen the gate; improve the strategy or the execution model.
