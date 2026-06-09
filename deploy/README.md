# Deploy hl-bot 24/7

One-command, idempotent deploy to a fresh Ubuntu/Debian host. Runs as a
locked-down `hlbot` system user under systemd timers. **Defaults to paper** —
going live is a separate, gated step ([`../docs/GO_LIVE.md`](../docs/GO_LIVE.md)).
Architecture rationale is in [`../docs/INFRA.md`](../docs/INFRA.md).

## Install / update

```bash
sudo REPO_URL=https://github.com/<you>/hl-bot.git BRANCH=main bash deploy/install.sh
```

Re-run any time to update to the latest branch HEAD. What it does:
1. installs `git`, `curl`, `uv`; creates the `hlbot` system user;
2. clones/updates the repo into `/opt/hl-bot`, runs `uv sync`;
3. creates `/etc/hl-bot/env` (chmod 600) from `env.example` — **edit this**;
4. installs + enables the systemd timers (tick every 5 min, report daily);
5. optionally sets up Litestream backups (if `LITESTREAM_S3_BUCKET` is set);
6. runs `hlbot doctor` preflight.

## What runs

| Unit | Cadence | Does |
|---|---|---|
| `hlbot-tick.timer` → `.service` | every 5 min | `deploy/run-tick.sh`: ingest → agents (paper unless `HLBOT_TICK_ARGS`) → supervisor → health |
| `hlbot-report.timer` → `.service` | daily 13:00 UTC | Telegram report + `track-record` export |
| `hlbot-ws.service` | continuous | WebSocket feed → snapshot the tick overlays (sub-second mids, L2, live liquidations); REST fallback when stale |
| `hlbot-recorder.service` | continuous | forward-records the WS trades stream into a fine-cadence candle archive (`data/recorder/`) so a months-long 1m/5m dataset accumulates for sub-bar research; records public data only |
| `hlbot-loop.service` | continuous | the Ralph self-improvement loop — **not auto-enabled**; `systemctl enable --now hlbot-loop` once `claude` is authed |

## Operate

```bash
systemctl list-timers 'hlbot-*'        # schedule
journalctl -u hlbot-tick -f            # live logs
sudo -u hlbot bash -c 'cd /opt/hl-bot && uv run hlbot health'   # ad-hoc health
sudo -u hlbot bash -c 'cd /opt/hl-bot && uv run hlbot doctor'   # preflight
```

Monitoring: set `HEALTHCHECK_URL` (e.g. Healthchecks.io) in `/etc/hl-bot/env` —
each tick's `hlbot health` pings it when healthy, so a missed ping pages you;
`warn`/`down` also Telegram-alerts.

## Going live (gated)

1. Confirm a strategy on real history:
   `sudo -u hlbot bash -c 'cd /opt/hl-bot && uv run hlbot backtest-fetch ... && uv run hlbot confirm --agent <a> --prefer maker'`
2. Place the API wallet at `~hlbot/.config/hermes/hl-bot-api-wallet.env` (chmod 600).
3. Enable the agent to `live_small` in `agent_state` (see `docs/GO_LIVE.md`).
4. Set `HLBOT_TICK_ARGS="--live --execution maker"` in `/etc/hl-bot/env`, then
   `systemctl restart hlbot-tick.timer`. Watch the first ticks closely.

## Kill switch

```bash
# stop new entries instantly
sudo systemctl stop hlbot-tick.timer
# or revert to paper without stopping:
sudo sed -i 's/^HLBOT_TICK_ARGS=.*/HLBOT_TICK_ARGS=/' /etc/hl-bot/env
```
