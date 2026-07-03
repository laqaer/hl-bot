# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-agent trading harness for Hyperliquid perps with **auto-promotion**: strategy agents earn their way from `paper` → `live_small` → `live` by passing evidence gates, and the supervisor flips the modes itself. Default mode is paper trading; live orders require the deliberate `--live` switch and are human-gated.

## Commands

Everything runs through `uv` — bare `python3`/`pytest` uses system Python, which lacks the dependency tree (eth_account, hyperliquid, pandas, ...).

```bash
uv sync                                    # install deps (Python 3.11)
make test        # = uv run pytest -q
make lint        # = uv run ruff check src tests scripts
make check       # lint + test — matches CI exactly
uv run pytest tests/test_runner.py -q      # single test file
uv run pytest -q -k test_name              # single test by name
```

The CLI entrypoint is `hlbot` (`src/hl_bot/cli/main.py`). Common ops: `uv run hlbot init | run | ingest | score | supervisor | report | backtest | confirm | sweep | ws | doctor | health | kill | resume`.

## Architecture

Four layers, bottom-up (data flows one way, toward the promotion gates):

1. **Runtime/Ingest** — `engine/runner.py` runs one cycle for every agent: build roster from `configs/*.yaml` → reconcile positions → allocate caps → ask each agent to `decide()` → paper-simulate paper agents (`sim/paper.py`) → execute live agents through `exec/` (maker lifecycle by default, taker on demand). `ingest/hyperliquid.py` pulls real fills/funding/equity; `ingest/ws.py` is the WebSocket market-data service.
2. **Ground truth (SQLite)** — `db/schema.py`; DB at `data/hlbot.sqlite` (gitignored). Tables: fills, equity_snapshots, funding_payments, agent_decisions, positions, agent_state, goal_evaluations. Schema changes need migrations (see `test_migrations.py`).
3. **Scoring** — `scoring/metrics.py` builds per-agent rolling scorecards (1h/24h/7d/30d/all): net_pnl, sharpe, max_dd, win_rate, edge_bps, profit_factor. Fill→agent attribution works via client order IDs (`agents/cloid.py::make_cloid`).
4. **Supervisor** — `supervisor/loop.py` evaluates each agent's goals/guardrails/promotion ladder from its YAML contract and promotes/demotes/pauses.

Key design invariants:

- **Agents never place orders.** An agent implements `Agent.decide(view) -> list[Decision]` (`agents/base.py`); the runtime places orders so paper/live routing, guardrails, kill-switch checks, and logging stay centralized in one audited path (`exec/router.py`).
- **Backtest and production share scoring code.** The backtest engine (`backtest/engine.py`) drives each agent's real `decide()` and scores via the same `score_agent` used live, so numbers can never silently disagree.
- **`hlbot run` is the one engine.** Paper agents keep deciding and simulating during live operation — that's what feeds the promotion gates evidence. (`femr_tick` is deprecated.)
- **Profiles are hard walls.** `HLBOT_PROFILE` (e.g. the moonshot sleeve) gets its own data dir/DB/KILL file, its own `configs/<profile>/` set, and may sign with a different API wallet (`config.py::Settings`).

## Adding an agent (the strategy pipeline)

Full workflow in `docs/STRATEGY_PIPELINE.md`. In code:

1. Spec in `docs/research/<name>.md` (template: `docs/research/SPEC_TEMPLATE.md`) — must state costs first and which execution mode it assumes.
2. Agent in `src/hl_bot/agents/<name>.py` implementing `decide(view)`; set `default_execution` ("maker" for patient carry, "taker" for urgency-driven; exits are always taker).
3. Register a factory in `engine/runner.py::AGENT_FACTORIES` (single source of truth for the roster).
4. YAML contract in `configs/<name>.yaml`: mode, goals, guardrails, promotion ladder (see `funding_arb_v1.yaml` for the shape).
5. Unit tests in the established synthetic-frames style; optionally a sweep spec in `configs/sweeps/<name>.yaml` for the nightly grid (results land in `research/results/`).

## Safety rules — do not weaken

- **Promotion gates are CI-enforced.** `tests/test_gate_minima.py` encodes minimum gate strictness for all `configs/*.yaml`; loosening a threshold below the minima fails CI and requires a deliberate human edit to that test file alongside it.
- **Never enable or scale live trading autonomously.** Going live is a human decision (`docs/GO_LIVE.md`). The ralph loop is explicitly forbidden from enabling live capital, raising notional caps, or running live ticks.
- The kill switch (`hlbot kill`, `data/KILL`) is sticky and checked at cycle start and before every order placement — keep it that way in any execution-path change.
- A strategy change isn't "done" without a backtest number as evidence.

## The ralph loop

`ralph/` is an autonomous self-improvement loop: each iteration picks the top unblocked item from `ralph/BACKLOG.md`, makes one tested change, gates on green `pytest` + `ruff`, commits, and journals to `ralph/PROGRESS.md`. If you're working an item from the backlog, follow `ralph/PROMPT.md` and log to `PROGRESS.md`.

## Repo map (non-obvious parts)

- `configs/` — per-agent goal contracts; `agent_overrides.json` (runtime param overrides), `sweeps/` (nightly grid specs), `moonshot/` (profile-specific contracts)
- `deploy/` — one-command idempotent host deploy: `install.sh`, systemd units/timers, Litestream backup config, Terraform for AWS (`deploy/aws/`)
- `research/results/` — committed nightly sweep outputs; read these before tuning strategy params
- `docs/` — `GO_LIVE.md` (live gating runbook), `STRATEGY_PIPELINE.md`, `INFRA.md`, `REVIEW.md`

## Conventions

- Python 3.11, ruff (line-length 100; rules E,F,I,B,UP,SIM; E501 ignored), pydantic v2, typer CLI.
- Config/env via `config.py::Settings.from_env()` (loads `.env`; see `.env.example`). `HL_SECRET_KEY` is only needed for live; paper/read-only needs just `HL_ADDRESS`. `HLBOT_PAPER=1` is the default.
- `data/`, `*.sqlite`, and `.env` are gitignored — never commit them.
