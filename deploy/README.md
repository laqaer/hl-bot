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

The mean-reversion VWAP window defaults to 60×1m (the historical config). To
flip it (e.g. to the 4h window once B-G014's multi-week evidence confirms
B-WIN), append `--vwap-window 240` to `HLBOT_TICK_ARGS` or set
`HLBOT_VWAP_WINDOW=240` in `/etc/hl-bot/env`. Backtest the exact window first:
`hlbot confirm ... --vwap-window 240`.

## Paper book (forward-testing candidates)

A paper tick (`hlbot femr_tick` without `--live`) logs its place/flatten
decisions as paper rows (`is_paper=1`), so paper agents track positions,
exits, and cooldowns exactly like live — that paper book is the forward-test
evidence for promoting a candidate. Replays are book-aware (a live tick never
acts on paper rows and vice versa), so sharing one DB is safe; still, to keep
the live DB clean, prefer a dedicated file when running a paper loop next to a
live one:

```bash
sudo -u hlbot bash -c 'cd /opt/hl-bot && HLBOT_DB=data/hlbot_paper.sqlite uv run hlbot femr_tick'
```

Note the live tick in live mode runs only promoted agents — paper evidence for
a candidate accumulates only where paper ticks actually run.

The paper roster includes `breakout_v1` (96h Donchian channel on 15m bars,
B-EDGE2a), which adds one 15m candleSnapshot call per top-20 coin to each
paper tick; live ticks skip the feed entirely unless breakout is promoted in
`agent_state`.

Read the paper book with `hlbot score --paper` (B-PAPER3): it replays the
paper place/flatten rows into scorecards under the backtester's taker cost
model (fees + slippage), so the numbers are comparable to G0 backtests, and
lists still-open paper positions. `funding_pnl` is *modeled* (B-PAPER3a): the
command fetches HL funding-rate history over each paper hold and accrues the
engine's `-signed × notional × rate` per hourly event, marked at the entry
mid — so femr's revenue line is visible; pass `--no-funding` to skip the
network calls (funding reads 0 then; a per-coin fetch failure also degrades
to 0 with a warning). Still realized-only on price: an open position shows
entry fees and accrued funding but no mark-to-market. Promotion stays
human-gated; this is the evidence readout, not an auto-promoter.

```bash
sudo -u hlbot bash -c 'cd /opt/hl-bot && HLBOT_DB=data/hlbot_paper.sqlite uv run hlbot score --paper'
```

`hlbot track-record` includes the same paper cards as a clearly-labeled
"Paper agents (NOT live)" section (B-PAPER3b) in all three exports
(json/md/html) — paper-only agents never appear in the live per-agent table,
and the account equity curve stays fills-based. `--no-paper-funding` skips
the funding-rate fetches like `score --no-funding`.

## Kill switch

```bash
# stop new entries instantly
sudo systemctl stop hlbot-tick.timer
# or revert to paper without stopping:
sudo sed -i 's/^HLBOT_TICK_ARGS=.*/HLBOT_TICK_ARGS=/' /etc/hl-bot/env
```
