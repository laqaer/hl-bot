# Go-live runbook & checklist

How hl-bot goes to production **safely**. "Live" is a *human* decision gated on
evidence — not a flag the autonomous loop flips. This doc is the contract for
crossing that line. It is deliberately conservative: the strategies have shown
*negative* edge after costs, so going live before that is fixed just loses money
faster.

## The one rule

> **Do not enable or scale live trading until a strategy has passed G0→G2 in
> [`ROADMAP_TO_1M.md`](ROADMAP_TO_1M.md).** No exceptions, no "just a small test
> to see." A negative-edge system at small size is still a negative-edge system.

## Pre-flight gates (all must be true)

- [ ] **G0 — Sim edge.** The strategy backtests **positive net-of-cost edge** and
  Sharpe > 1 over ≥90d real history (`hlbot backtest --compare`), and survives
  walk-forward + a 2–3× slippage stress (B5). Record the numbers in
  `ralph/PROGRESS.md`.
- [ ] **G1 — Paper.** ≥30d paper run: edge ≥ +5 bps, ≥150 trades, no guardrail
  breach. The supervisor's promotion gate for the agent's `*.yaml` reflects this.
- [ ] **Execution is maker-first.** Entries use `place_limit_order` (post-only);
  taker is reserved for urgent risk-reducing exits. (Without this, G0 won't pass —
  the spread eats the edge.)
- [ ] **Measurement is honest.** Funding is attributed per-agent (B6) and the
  agent's scorecard Sharpe/edge is real (B7); otherwise the gates are measuring
  an artifact.
- [ ] **Capital is sized for ruin-avoidance.** Live-small notional within the
  `min_bot_capital` / dynamic daily-loss / 5×-1× caps. Never raise a cap to
  recover a loss.

## Environment & secrets (live)

Live trading needs an API wallet the bot signs with — **never the main key**:

- `~/.config/hermes/hl-bot-api-wallet.env`, perms `0600`, containing:
  - `HL_BOT_API_PRIVATE_KEY=0x…` (64 hex) — an **API/agent wallet** approved on
    the trader account, not the funded key.
  - `HL_BOT_API_WALLET_ADDRESS=0x…` (derived address, must match the key).
- The trader (funded) account is `HL_TRADER_ADDRESS` in `exec/orders.py`.
  `build_exchange()` signs with the API wallet but acts on the trader account.
- `.env`: `HL_ADDRESS` (trader, read-only ops), optional `TG_BOT_TOKEN` /
  `TG_CHAT_ID` for alerts. `HLBOT_PAPER=1` keeps paper as the global default.

Sanity before any live tick:
```bash
uv run hlbot ingest        # confirms read access + populates fills/equity
uv run hlbot score         # confirms accounting looks right
uv run hlbot femr_tick     # PAPER by default: prints decisions, places nothing
```

## Promote an agent to live (the gated switch)

Agents are paper until **explicitly** enabled in `agent_state` AND in
`live_small`/`live` mode. The live tick (`femr_tick --live`) only executes agents
that pass `_filter_live_agents_by_state`.

1. Confirm the gates above. Re-read the agent's latest backtest + paper numbers.
2. Set the agent live-small (one agent at a time):
   ```sql
   -- in the bot DB (HLBOT_DB)
   INSERT INTO agent_state(agent, mode, enabled) VALUES('<agent>', 'live_small', 1)
     ON CONFLICT(agent) DO UPDATE SET mode='live_small', enabled=1;
   ```
   or let the supervisor promote it once its `*.yaml` promotion conditions pass.
3. First live tick, watched:
   ```bash
   uv run hlbot femr_tick --live      # places real orders for enabled agents only
   ```
   Verify the printed guardrail line is green, sizes match the allocator caps, and
   the first fills reconcile (`hlbot ingest && hlbot score`).
4. Only after a clean live-small window (G2) do you scale notional — and that
   happens automatically via the 5×/1× rule as portfolio value grows. Do not
   hand-raise caps.

## Monitoring (must be running before live)

- **Cron** (see README): `tick`/`ingest`/`supervisor` every 5 min; daily report.
- **Daily scorecard** to Telegram (`scripts/daily_scorecard.py`).
- **Alerts** (`telegram_alert`) on guardrail trips, repeated rejections, big PnL
  moves — confirm `TG_BOT_TOKEN`/`TG_CHAT_ID` resolve.
- Watch: 24h realized PnL vs `max_daily_loss`, open notional vs cap, rejection
  rate, and reconciliation events (stale ownership clears).

## Kill switch & rollback

- **Halt new entries immediately:** the supervisor pauses an agent on its 24h
  loss guardrail; to force it:
  ```sql
  UPDATE agent_state SET enabled=0, mode='paper', paused_reason='manual halt' WHERE agent='<agent>';
  ```
  Paused/paper agents place nothing; **flatten/close decisions still run** for
  risk reduction.
- **Flatten everything:** stop the cron, then run the live tick once — open
  positions hit their exit logic; or close manually on HL. Reconciliation will
  clear stale DB ownership next tick.
- **Roll back params:** `configs/agent_overrides.json` is the live-tuning file;
  revert it in git to undo an auto-tuner change.
- **Full stop:** disable the cron / systemd timer. No process = no orders.

## Vault retargeting (CAPITAL.md Track A — human-gated, G3 first)

Point the bot at a Hyperliquid vault instead of the personal account. One env
var does it — `HL_VAULT_ADDRESS` — and it must be that var: it makes the bot
sign every exchange action with `vaultAddress` (orders execute on the vault)
*and* routes every account read (fills/funding/equity ingest, guardrail
capital, open orders) to the vault. Setting only `HL_TRADER_ADDRESS` to a
vault would read the vault while orders quietly execute on the personal
account. A malformed value refuses to run rather than fall back.

Checklist, in order:

1. **Gate:** G3 PASS in `hlbot gates` + a published `hlbot track-record`
   artifact. No vault before the record exists (CAPITAL.md Track A).
2. **Create the vault** from the leader (master) account in the HL UI; record
   the vault address. Verify the current creation fee/terms in HL docs first —
   they change.
3. **Seed** the leader's ≥5% of TVL (HL requirement).
4. **Flatten first:** the bot must hold no open positions and no resting
   orders before the switch. Decision-log ownership is account-agnostic, so
   retargeting with an open book would make reconciliation read the old
   account's positions as stale (it force-flattens state — safe direction,
   but messy and it orphans real positions on the personal account).
5. **API wallet:** the existing approved API wallet of the leader signs vault
   actions — no new key. (Verify this still holds in current HL docs.)
6. Set `HL_VAULT_ADDRESS=0x...` in `/etc/hl-bot/env`; restart the units.
7. `hlbot doctor`: the `vault` check must read "retargeted: orders sign
   vaultAddress=…" and `hl_address` must show the vault.
8. **First live tick at tiny size, watched:** confirm the fill lands on the
   *vault's* trade history in the HL UI, not the personal account's. If it
   lands wrong, unset the var, restart, investigate — do not proceed.
9. `hlbot ingest` + `hlbot score` now account the vault book. Note: vault
   equity is perp-only, so the spot-USDC term of the capital guardrail reads
   $0 — tightening-only, no action needed.
10. **Rollback:** unset `HL_VAULT_ADDRESS`, restart. The personal account
    resumes; any positions left on the vault must be closed from the UI or by
    re-setting the var and letting exits run.

## What the autonomous loop may and may not do

- **May:** research, backtest, write code, paper-simulate, and *propose* config
  changes (always risk-reducing). It keeps CI green and the backlog moving.
- **May not:** set any agent to `live_small`/`live`, raise a notional cap, run
  `femr_tick --live`, or touch the API-wallet env. Those are human-only.

## Current readiness (2026-06-08)

**NOT ready for live.** Blockers: no strategy has passed G0 (need real-history
backtests, blocked by sandbox network — B1), maker execution exists as a
primitive but the live entry path is still synchronous taker (B2 follow-up), and
funding attribution (B6) / per-agent Sharpe (B7) aren't wired yet. The regime
TWAP (B3) is the leading G0 candidate. Path to ready = work the P0/P1 backlog.
