# Go-live runbook & checklist

How hl-bot goes to production. Since the 2026-06 overhaul, **promotion is
automatic** — the supervisor walks each agent paper → live_small → live as its
gates pass — but two things stay human: flipping the engine's global live
switch (`HLBOT_RUN_ARGS`), and clearing the kill switch after a trip. This doc
is the runbook for both, plus the host-side validation sequence (the "P4"
steps) that turns the overhaul into live PnL.

## The one rule

> **Capital follows evidence.** The gates (G0 confirm stamp, paper soak,
> real-fill live_small window) are encoded in `configs/*.yaml` and their
> minima are enforced by CI (`tests/test_gate_minima.py`). Don't loosen a
> gate to get a strategy through it; fix the strategy or the execution.

## Host bring-up sequence (do these in order)

```bash
# 0. Deploy (idempotent) — installs hlbot-run/-ws/-report/-sweep units, paper mode
sudo REPO_URL=https://github.com/<you>/hl-bot.git BRANCH=main bash deploy/install.sh
uv run hlbot doctor

# 1. WS feed on the trading universe (also starts accruing data/liq_log.jsonl)
#    /etc/hl-bot/env: HLBOT_WS_COINS=BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE
systemctl restart hlbot-ws

# 2. THE HIGHEST-VALUE STEP — fetch real history and confirm the carry class
#    (this was blocked for the entire life of the repo; it answers whether the
#    one plausibly-positive strategy class clears G0 with maker costs):
uv run hlbot backtest-fetch --coins BTC,ETH,SOL,HYPE,DOGE,XRP --interval 1h --days 180
uv run hlbot confirm --agent xfund_carry_v1   --prefer maker --days 180 --record
uv run hlbot confirm --agent funding_carry_v1 --prefer maker --days 180 --record
#    --record stamps the confirmations table: this is what require_g0 checks.
#    If NOT CONFIRMED: do not go live; the nightly sweep + research pipeline
#    (docs/STRATEGY_PIPELINE.md) is the path forward, not a smaller gate.

# 3. Paper soak: hlbot-run is already running paper. Give it ≥5 days (compressed gates; min_days floor is 3); verify
uv run hlbot score          # paper agents show n_trades, edge, non-None sharpe
uv run hlbot report         # funding_pnl visible per agent
journalctl -u hlbot-run -n 50   # cycles ticking, supervisor evaluating

# 4. Flip the global live switch (the human step):
#    /etc/hl-bot/env -> HLBOT_RUN_ARGS="--live --execution maker"
systemctl restart hlbot-run
#    Auto-promotion does the rest: agents whose ladders pass go live_small at
#    sizing caps (~$75 total / $25 per trade). WATCH THE FIRST FILLS:
journalctl -u hlbot-run -f      # RESTING/FILLED/REPRICED events
uv run hlbot ingest && uv run hlbot score
uv run hlbot track-record    # the shareable artifact starts here
```

## What auto-promotion will and won't do

- **Will:** flip `agent_state.mode` paper→live_small→live when an agent's
  ladder passes (paper evidence + fresh G0 stamp for stage 1; **real fills
  only** for stage 2), respecting `min_days_in_mode`; Telegram-alert every
  promotion/demotion/pause; demote/pause automatically on guardrail breach.
- **Won't:** trade an agent the global `--live` switch isn't set for; promote
  while the kill switch is active; exceed mode sizing caps, the 5×/1× rule,
  or order-rate limits (20/h per agent, 60/h account).

## Environment & secrets (live)

Live trading signs with an API wallet — **never the funded key**:

- `~hlbot/.config/hermes/hl-bot-api-wallet.env`, perms `0600`:
  `HL_BOT_API_PRIVATE_KEY=0x…` + `HL_BOT_API_WALLET_ADDRESS=0x…`
- `/etc/hl-bot/env`: `HL_ADDRESS` / `HL_TRADER_ADDRESS` (funded account),
  `TG_BOT_TOKEN`/`TG_CHAT_ID` (alerts), `HEALTHCHECK_URL` (dead-man),
  `HLBOT_RUN_ARGS` (the live switch), `HLBOT_WS_*`.
- Moonshot sleeve: separate sub-account + wallet in `/etc/hl-bot/moonshot.env`
  — see [`MOONSHOT.md`](MOONSHOT.md).

## Kill switch & rollback (know these before going live)

- **Halt everything, sticky:** `uv run hlbot kill "reason"` (or `touch
  data/KILL` over SSH). New entries and promotions stop; flatten/cancel still
  run. Trips automatically on: account 24h-loss breach, equity < 75% of 30d
  high-water-mark. **Only a human runs `hlbot resume`.**
- **Halt one agent:** the supervisor pauses it on its 24h-loss guardrail; to
  force: `UPDATE agent_state SET enabled=0, mode='paper',
  paused_reason='manual halt' WHERE agent='<agent>';`
- **Flatten everything:** `systemctl stop hlbot-run`, close on HL manually if
  urgent; reconciliation clears stale DB ownership on restart.
- **Roll back params:** `configs/agent_overrides.json` is the live-tuning
  file; revert in git.
- **Full stop:** `systemctl stop hlbot-run hlbot-ws` — no process, no orders.

## Monitoring (must be green before and during live)

- `hlbot health` (timer/manual): tick freshness, ingest age, kill state,
  paused agents, 24h PnL; pings `HEALTHCHECK_URL` (dead-man) when ok.
- Telegram: daily report, guardrail trips, every promotion/demotion, kill
  trips.
- Watch in week one of maker execution: **maker fill rate** (target >30%),
  taker-fallback rate, reprice counts, realized px vs quote (journalctl
  events; telemetry hardening tracked as backlog E1).

## What the autonomous loop (ralph) may and may not do

- **May:** research, implement specs, run sweeps/backtests, tighten configs,
  improve execution quality, keep CI green.
- **May not:** write `agent_state`, weaken any gate/cap/limit (CI-enforced),
  touch KILL files, or handle wallet material. See `ralph/PROMPT.md`.

## Current readiness (2026-06-11, post-overhaul)

Code-ready, **evidence-pending**: the engine, measurement, auto-promotion,
safeguards and sleeve are built and tested (198 tests). The remaining blockers
are host-side facts, not code (api.hyperliquid.xyz returns 403 from CI and
sandboxes, so a networked host is required): run step 2 above (first-ever real-history
confirmation of the carry class), then the paper soak. If carry confirms, the
system takes itself live and scales as gates pass; if it doesn't, the research
pipeline is the next move and no capital goes live on an unconfirmed strategy.
