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

## Backtesting

```bash
# Replay an agent over real HL history with an explicit cost model.
# --compare runs taker AND maker so you can see how much edge the spread eats.
uv run hlbot backtest --agent twap_mr_v1 --coins BTC,ETH,SOL --days 30 --compare
```

The engine (`src/hl_bot/backtest/`) drives each agent's real `decide()`, simulates
fills with fee + slippage + funding, and scores via the same `score_agent` used
live — so backtest and production numbers can never silently disagree.

## Strategy, review & the self-improvement loop

- [`docs/REVIEW.md`](docs/REVIEW.md) — full code/system review and findings.
- [`docs/ROADMAP_TO_1M.md`](docs/ROADMAP_TO_1M.md) — the numbers-first path to a
  $1M portfolio and the promotion gates.
- [`ralph/`](ralph/) — an autonomous loop that works the prioritized
  [`ralph/BACKLOG.md`](ralph/BACKLOG.md): research → backtest → propose. Going
  live stays human-gated.

## Deploy

One-command, idempotent 24/7 deploy (systemd timers, Litestream backups,
health/heartbeat, optional self-improvement loop). Defaults to **paper**; going
live is gated. See [`deploy/README.md`](deploy/README.md) and
[`docs/INFRA.md`](docs/INFRA.md).

```bash
# Any Ubuntu host:
sudo REPO_URL=https://github.com/<you>/hl-bot.git BRANCH=main bash deploy/install.sh
# AWS (one apply -> EC2 already running paper): see deploy/aws/
#   cd deploy/aws && terraform init && terraform apply -var key_name=... -var hl_address=0x...
uv run hlbot doctor    # preflight: env, DB, configs, API-wallet, HL reachability
uv run hlbot health    # ok/warn/down + heartbeat ping
uv run hlbot ws        # WebSocket market-data service (writes a snapshot)
```
End-to-end runbook: [`docs/HOST_QUICKSTART.md`](docs/HOST_QUICKSTART.md). AWS:
[`deploy/aws/README.md`](deploy/aws/README.md). **Novice set-and-forget on AWS
(auto self-improving):** [`docs/AWS_NOVICE_SETUP.md`](docs/AWS_NOVICE_SETUP.md).

## Roadmap

- [x] Backtest harness re-using the same scoring code.
- [x] Strategy confirmation gate (`hlbot confirm` — walk-forward + cost stress).
- [x] Maker (post-only) execution + cross-tick fill lifecycle (`--execution maker`).
- [x] WebSocket market view + live liquidations feed (`hlbot ws`).
- [x] 24/7 deployment automation + health/heartbeat + preflight.
- [x] Carry strategies: `xfund_carry_v1` (market-neutral), `funding_carry_v1`.
- [ ] Per-agent position attribution + funding from fills (replay engine) — B6/B7.
- [ ] Book-aware maker pricing (post at touch/microprice using WS L2) — B-book.
- [ ] LLM-reasoning agent template with auto-captured thought traces.
