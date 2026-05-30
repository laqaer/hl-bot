# hl-bot

Multi-agent trading harness for [Hyperliquid](https://hyperliquid.xyz) with a built-in
**goal-tracking and promotion system** for evaluating whether agents are actually making money.

> 🛡️ **Default mode is paper trading.** Live order placement is intentionally not wired —
> see [`agents/runtime.py`](src/hl_bot/agents/runtime.py). Flip the switch only after a
> conscious review.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Layer 3 — Supervisor                                              │
│    goals.yaml ─► evaluate scorecards ─► promote / demote / pause   │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│  Layer 2 — Scoring                                                 │
│    per-agent rolling scorecards (1h/24h/7d/30d/all):               │
│    net_pnl, sharpe, max_dd, win_rate, edge_bps, profit_factor      │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│  Layer 1 — Ground truth (SQLite)                                   │
│    fills · equity_snapshots · funding_payments · agent_decisions   │
│    positions · agent_state · goal_evaluations                      │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│  Runtime — fetch market view, ask each agent, log decisions        │
│  Ingest — pull Hyperliquid userFills / userFunding / clearinghouse │
└────────────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
uv sync
cp .env.example .env  # fill in HL_ADDRESS
uv run hlbot init
uv run hlbot tick                  # paper run, 1 tick, all agents
uv run hlbot ingest                # pull fills + equity from HL
uv run hlbot score                 # show scorecards
uv run hlbot supervisor            # evaluate goals/guardrails
uv run hlbot report                # markdown daily report
uv run hlbot report --send         # also push to Telegram
```

## Adding an agent

1. Implement `Agent.decide(view) -> list[Decision]` in `src/hl_bot/agents/`.
2. Tag orders with `cloid = make_cloid(self.name)` so fills are attributable.
3. Drop a goals file in `configs/<agent>.yaml` (see `funding_arb_v1.yaml`).
4. Add it to the agent list in `cli/main.py::tick`.

## Goals contract

```yaml
agent: my_agent
mode: paper
goals:
  primary:   {metric: sharpe,  window: 30d, op: ">=", threshold: 1.5}
  secondary:
    - {metric: net_pnl, window: 30d, op: ">=", threshold: 500}
guardrails:
  - {metric: net_pnl, window: 24h, op: ">=", threshold: -200,
     action: pause,  reason: "24h loss limit"}
promotion:
  from: paper
  to: live_small
  conditions:
    - {metric: sharpe,   window: 30d, op: ">=", threshold: 2.0}
    - {metric: n_trades, window: 30d, op: ">=", threshold: 100}
```

Supported metrics: anything on `Scorecard` (`net_pnl`, `sharpe`,
`max_drawdown`, `win_rate`, `edge_bps`, `profit_factor`, `n_trades`, …).

## Schedule it

```bash
# Every 5 min: tick, ingest, supervise
*/5 * * * * cd ~/projects/hl-bot && uv run hlbot tick && uv run hlbot ingest && uv run hlbot supervisor

# Daily 09:00: Telegram report
0  9 * * * cd ~/projects/hl-bot && uv run hlbot report --send
```

## Roadmap

- [ ] Live order adapter (signed `hyperliquid-python-sdk` exchange.order) — gated behind `HL_SECRET_KEY` + `mode != paper`.
- [ ] Per-agent position attribution from fills (replay engine).
- [ ] WebSocket market view for sub-second ticks.
- [ ] Backtest harness re-using the same scoring code.
- [ ] LLM-reasoning agent template with auto-captured thought traces.
