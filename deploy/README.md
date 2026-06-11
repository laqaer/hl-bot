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
| `hlbot-loop.service` | continuous | the Ralph self-improvement loop, in a **separate clone** (`/opt/hl-bot-loop`) — set up with `deploy/setup-loop.sh` (below), not auto-enabled |

### Self-improvement loop (always-on, off the live dir)

Run the loop on this box (or any VPS) so it improves the bot 24/7 without your Mac.
It lives in a **separate clone** so the autonomous agent never touches the live
trading files — the live bot only auto-deploys the clean commits it pushes.

```bash
sudo GITHUB_TOKEN=ghp_xxx \
     REPO_URL=https://github.com/laqaer/hl-bot.git \
     BRANCH=claude/gracious-fermat-g1QZ4 \
     bash deploy/setup-loop.sh
# then follow its two prompts: `claude setup-token` (OAuth) + `systemctl enable --now hlbot-loop`
```
`GITHUB_TOKEN` = a fine-grained PAT with **Contents: Read+Write** on the repo (so the
loop can push). The loop uses your Claude **subscription** (OAuth, no API billing).

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

## Host sizing & "I can't SSH in"

`install.sh` adds a swapfile (`deploy/ensure-swap.sh`) and the units bound the
self-improvement loop's memory + shield the trader/feed from the OOM killer, so a
runaway process can't wedge the box. If SSH ever fails with
`kex_exchange_identification: read: Connection reset by peer` (TCP connects, then
resets), sshd accepted the socket but couldn't serve you — the usual causes, in
order: **(1)** out of memory (`free -m`; add swap: `sudo bash deploy/ensure-swap.sh`),
**(2)** full root disk (`df -h /`), **(3)** a broken sshd config or socket-activated
`ssh.service` that failed (`sudo sshd -t`, `systemctl status ssh ssh.socket`),
**(4)** fail2ban/firewall banning your IP (`sudo fail2ban-client status sshd`).
On AWS, **SSM Session Manager** gets you a shell even when sshd is down (attach an
IAM role with `AmazonSSMManagedInstanceCore`). The bot keeps trading via systemd
regardless of shell access. Run the loop on its **own** small VPS (or a ≥4GB box)
so the trader stays light.

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
