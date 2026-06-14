# Host quickstart — from nothing to running 24/7

End-to-end runbook to stand the bot up on a VPS. It comes up in **paper**; going
live is the deliberate, gated step at the end. See also [`INFRA.md`](INFRA.md)
(architecture/cost), [`../deploy/README.md`](../deploy/README.md) (the installer),
and [`GO_LIVE.md`](GO_LIVE.md) (the live gate + kill switch).

## 0. Host

Cheapest solid picks (1 vCPU / 1–4 GB is plenty): Hetzner CX22 (~€4.5), a
Vultr/Linode **Tokyo** 1 GB (~$5, lower HL latency), or Oracle Cloud free tier.
Ubuntu 22.04/24.04 + a sudo user.

> **If the repo is private**, the host needs git access (HTTPS token or deploy
> key) for the clone in step 1.

## 1. Deploy (one command — comes up PAPER)

Either merge to `main` first and use `BRANCH=main`, or deploy a branch directly:

```bash
git clone -b main https://github.com/laqaer/hl-bot.git /tmp/hl-bot-src
sudo REPO_URL=https://github.com/laqaer/hl-bot.git BRANCH=main \
     bash /tmp/hl-bot-src/deploy/install.sh
```

Installs `uv`, a locked-down `hlbot` user, the repo at `/opt/hl-bot`,
`/etc/hl-bot/env`, the systemd units (tick every 5 min, daily report, **WS feed**),
optional Litestream backups, then runs a preflight.

## 2. Configure

```bash
sudo nano /etc/hl-bot/env
#   HL_ADDRESS / HL_TRADER_ADDRESS  -> YOUR funded account
#   TG_BOT_TOKEN / TG_CHAT_ID       -> alerts
#   HEALTHCHECK_URL                 -> dead-man switch (e.g. Healthchecks.io)
#   LITESTREAM_S3_BUCKET            -> optional DB backups
sudo systemctl restart hlbot-tick.timer hlbot-ws.service
```

## 3. Verify

```bash
systemctl list-timers 'hlbot-*'
systemctl status hlbot-ws.service --no-pager
journalctl -u hlbot-tick -f                  # watch a tick (Ctrl-C to stop)
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run hlbot doctor'
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run hlbot health'
```

## 4. THE decisive step — confirm edge on real data (read-only, no keys)

```bash
sudo -u hlbot bash -lc 'cd /opt/hl-bot && \
  uv run hlbot backtest-fetch --coins BTC,ETH,SOL,HYPE,AVAX,LINK --days 120 && \
  uv run hlbot confirm --agent xfund_carry_v1    --prefer maker && \
  uv run hlbot confirm --agent funding_carry_v1  --prefer maker && \
  uv run hlbot confirm --agent twap_mr_regime_v1 --prefer taker'
```

Each prints **✅ CONFIRMED / ❌ NOT CONFIRMED** with walk-forward + a cost ladder.
This is the fork: a ✅ earns the path to live; all ❌ means no tradeable edge at
this size (the lever becomes capital / AUM via `hlbot track-record`).

## 5. Go live (only after a ✅, deliberately)

The agents wired into the live tick today: `twap_mr_regime_v1`, `femr_v1`,
`twap_mr_v1`, `liq_cascade_v1`, `basis_v1`. The **carry agents are confirm-only**
until promoted into the roster — if one wins, that's a one-line gated change.

```bash
# a) API/agent wallet (created & approved on HL — NOT your funded key)
sudo -u hlbot mkdir -p /home/hlbot/.config/hermes
sudo -u hlbot tee /home/hlbot/.config/hermes/hl-bot-api-wallet.env >/dev/null <<'EOF'
HL_BOT_API_PRIVATE_KEY=0xYOUR_API_WALLET_KEY
HL_BOT_API_WALLET_ADDRESS=0xYOUR_API_WALLET_ADDR
EOF
sudo chmod 600 /home/hlbot/.config/hermes/hl-bot-api-wallet.env

# b) enable the confirmed agent to live_small
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run python -c "
from hl_bot.config import Settings; from hl_bot.db.schema import init_db
c=init_db(Settings.from_env().db_path)
c.execute(\"INSERT INTO agent_state(agent,mode,enabled) VALUES(?,?,1) ON CONFLICT(agent) DO UPDATE SET mode=excluded.mode,enabled=1\",(\"twap_mr_regime_v1\",\"live_small\"))
print(\"enabled twap_mr_regime_v1 live_small\")"'

# c) flip the tick to live maker, restart, and WATCH the first ticks
sudo sed -i 's|^HLBOT_TICK_ARGS=.*|HLBOT_TICK_ARGS=--live --execution maker|' /etc/hl-bot/env
sudo systemctl restart hlbot-tick.timer
journalctl -u hlbot-tick -f
```

Confirm the guardrail line is green and sizes match the caps. Notional is tiny by
design (capital floor ~$40, per-trade caps).

## 6. Operate & kill switch

```bash
# back to paper instantly (keeps running):
sudo sed -i 's|^HLBOT_TICK_ARGS=.*|HLBOT_TICK_ARGS=|' /etc/hl-bot/env
sudo systemctl restart hlbot-tick.timer
# full stop:
sudo systemctl stop hlbot-tick.timer
```

Health pings `HEALTHCHECK_URL` each tick; `warn`/`down` Telegram-alerts.

## 7. Optional: the self-improvement loop

The Ralph loop (`hlbot-loop.service`) runs the `claude` CLI headlessly to
research / backtest / propose — it **never enables live trading**. It runs as
the **`hlbot`** system user, so it needs `claude` installed and authenticated
*for that user* (OAuth credentials are per-user). Do this before enabling:

```bash
# a) claude on the host, reachable by the service. Verify it's on the systemd
#    PATH (/usr/local/bin:/usr/bin:…); if not, pin CLAUDE_BIN in the env (below).
command -v claude || npm install -g @anthropic-ai/claude-code

# b) auth WITHOUT metered API billing: mint a 1-year Max/Pro OAuth token.
#    Run this in a shell where you can open a browser link, then copy the token.
claude setup-token

# c) hand the token (and, if needed, the claude path) to the service via the
#    env file the unit already loads. Use your OAuth sub, NOT an API key —
#    if ANTHROPIC_API_KEY is set it OVERRIDES the token and bills per-token.
sudo tee -a /etc/hl-bot/env >/dev/null <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=paste-the-token-here
# CLAUDE_BIN=/usr/local/bin/claude   # only if claude isn't on the service PATH
# RALPH_PUSH=1                         # push green commits (needs git creds for hlbot)
EOF

# d) keep the loop OFF main — point the deployed checkout at a dev branch so its
#    commits never land on main (and a re-run of install.sh won't refuse).
sudo -u hlbot git -C /opt/hl-bot checkout -B claude/ralph-auto

# e) enable it (NOTE: sudo — systemctl needs root; "Interactive authentication
#    required" means you ran it unprivileged).
sudo systemctl enable --now hlbot-loop
sudo journalctl -u hlbot-loop -f          # watch; it auth-probes then loops
```

Stop it any time with `sudo systemctl disable --now hlbot-loop` (or `touch
/opt/hl-bot/ralph/STOP` for a graceful stop after the current iteration). The
loop fails fast if the token is missing/expired, caps each iteration at
`RALPH_TIMEOUT` (30 min) and aborts after `RALPH_MAX_FAILS` (5) stuck
iterations — see `ralph/README.md`.

> **Tip — fastest first run:** if you've already authed `claude` as your own
> sudo user, skip the systemd path and run the loop in your home on a dev
> branch: `tmux new -s ralph 'RALPH_ITERS=200 RALPH_PUSH=1 ralph/loop.sh'`. It's
> decoupled from the live engine and uses creds you already have.
