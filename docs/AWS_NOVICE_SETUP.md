# AWS set-and-forget setup (novice, step by step)

Goal: deploy once, then **don't touch it**. The bot runs 24/7, **improves its own
code automatically** (test-gated), self-governs risk, and messages you on Telegram.
This guide assumes you've never used AWS. ~30–45 min, mostly waiting.

> **Read this once, honestly:** "set and forget and make money" has two halves.
> The *infrastructure* is genuinely set-and-forget. The *making money* part is not
> guaranteed — it depends on a strategy having real edge, which isn't proven yet.
> What IS guaranteed: losses are **bounded** by hard guardrails (a daily-loss cap
> and a capital floor that halts trading), and the bot **starts in paper** (fake
> money) until you deliberately turn on live. So the worst case is "it trades tiny,
> loses a little, auto-pauses, and keeps trying to improve" — not "it blows up."

---

## What you'll give it access to (gather these first)

1. **An AWS account** — https://aws.amazon.com (credit card; ~$5–12/mo).
2. **Your Hyperliquid funded account address** (`0x…`) — public, safe to paste.
3. **A Hyperliquid API/agent wallet** — a *separate* key the bot signs with, that
   can trade but **cannot withdraw**. You create this in the Hyperliquid app
   (More → API). **Never give it your main wallet's seed phrase.** (Only needed
   for the live step — you can deploy in paper without it.)
4. **A Telegram bot + chat id** (optional but recommended for alerts) — message
   @BotFather to make a bot, and @userinfobot for your chat id.
5. **An Anthropic login** for the self-improvement loop (a Claude account or an
   `ANTHROPIC_API_KEY`).

---

## Part A — Deploy (paper, ~15 min)

### A1. Launch a server (AWS Console — easiest)
1. Sign in → search **EC2** → **Launch instance**.
2. **Name:** `hl-bot`.
3. **AMI:** *Ubuntu Server 24.04 LTS* → switch architecture to **64-bit (Arm)**.
4. **Instance type:** `t4g.small` (~$12/mo) or `t4g.micro` (free tier, fine if you
   won't run the self-improve loop on this box).
5. **Key pair:** *Create new key pair* → name it `hlbot-key` → **Download** the
   `.pem` (you need it to SSH).
6. **Network settings → Edit → Allow SSH from → My IP** (not "Anywhere").
7. **Configure storage:** bump to **20 GB gp3**.
8. **Advanced details → User data:** paste the script below **after editing the two
   lines marked CHANGE ME**:

```bash
#!/usr/bin/env bash
set -eux
apt-get update -y && apt-get install -y git curl
mkdir -p /etc/hl-bot
cat >/etc/hl-bot/env <<'ENV'
HL_ADDRESS=0xCHANGE_ME_YOUR_FUNDED_ADDRESS
HL_TRADER_ADDRESS=0xCHANGE_ME_YOUR_FUNDED_ADDRESS
HL_API_URL=https://api.hyperliquid.xyz
HLBOT_DB=/opt/hl-bot/data/hlbot.sqlite
HLBOT_HOME=/opt/hl-bot
HLBOT_PAPER=1
HLBOT_AUTO_UPDATE=1
HLBOT_TICK_ARGS=
HLBOT_WS_SNAPSHOT=/opt/hl-bot/data/ws_snapshot.json
HLBOT_WS_COINS=BTC,ETH,SOL,HYPE
HEALTHCHECK_URL=
TG_BOT_TOKEN=
TG_CHAT_ID=
ENV
git clone -b main https://github.com/laqaer/hl-bot.git /tmp/hl-bot-src
REPO_URL=https://github.com/laqaer/hl-bot.git BRANCH=main bash /tmp/hl-bot-src/deploy/install.sh
```

9. **Launch instance.** Wait ~3–4 minutes (it's installing in the background).

> Prefer infra-as-code? `deploy/aws/` has Terraform that does all of A1 with one
> `terraform apply` — see [`../deploy/aws/README.md`](../deploy/aws/README.md).

### A2. Connect and verify
- EC2 → Instances → select `hl-bot` → **Connect** → *EC2 Instance Connect* opens a
  browser terminal (no .pem needed). Or SSH: `ssh -i hlbot-key.pem ubuntu@<Public-IP>`.
- Check it's alive:
```bash
systemctl list-timers 'hlbot-*'
sudo -u hlbot bash -lc 'cd /opt/hl-bot && uv run hlbot health'
```
You should see timers scheduled and a 🟢/🟡 health line. **It's now running in
paper.** If you stop here, it costs you a few dollars a month and does nothing
risky.

---

## Part B — Turn on self-improvement (the "auto-improve, don't touch" part)

The box already **auto-deploys** improvements every 15 min (test-gated:
`HLBOT_AUTO_UPDATE=1`). To also let it **write** improvements, give it an Anthropic
key and start the loop:

```bash
# easiest: paste an API key from console.anthropic.com into the env, then start the loop
echo 'ANTHROPIC_API_KEY=sk-ant-CHANGE_ME' | sudo tee -a /etc/hl-bot/env
sudo systemctl enable --now hlbot-loop
# (alternative: `sudo -u hlbot -i; cd /opt/hl-bot; claude` to log in interactively)
```
Now: the loop researches + backtests + commits green improvements and pushes them;
the auto-update timer pulls them, re-runs the tests, and restarts the bot only if
green. **Fully hands-off, and it can never deploy broken code.**

---

## Part C — Go live (one deliberate decision, then forget)

Live trading needs two things only you can provide, and should only be turned on
for a strategy that has shown edge. Do this when you're ready to risk real (small)
money:

```bash
sudo -u hlbot -i ; cd /opt/hl-bot

# C1. Does any strategy actually have edge on real history? (read-only)
uv run hlbot backtest-fetch --coins BTC,ETH,SOL,HYPE,AVAX,LINK --days 120
uv run hlbot confirm --agent twap_mr_regime_v1 --prefer taker
uv run hlbot confirm --agent xfund_carry_v1    --prefer maker
#   -> only proceed for an agent that prints ✅ CONFIRMED

# C2. Give it the trading key (API/agent wallet — NOT your main seed)
mkdir -p ~/.config/hermes
cat > ~/.config/hermes/hl-bot-api-wallet.env <<'EOF'
HL_BOT_API_PRIVATE_KEY=0xYOUR_API_WALLET_PRIVATE_KEY
HL_BOT_API_WALLET_ADDRESS=0xYOUR_API_WALLET_ADDRESS
EOF
chmod 600 ~/.config/hermes/hl-bot-api-wallet.env

# C3. Enable the confirmed agent at small size
uv run python -c "
from hl_bot.config import Settings; from hl_bot.db.schema import init_db
c=init_db(Settings.from_env().db_path)
c.execute(\"INSERT INTO agent_state(agent,mode,enabled) VALUES(?,?,1) ON CONFLICT(agent) DO UPDATE SET mode=excluded.mode,enabled=1\",(\"twap_mr_regime_v1\",\"live_small\"))
print('live_small enabled')"
exit

# C4. Flip the bot to live (as your sudo user) and walk away
sudo sed -i 's|^HLBOT_TICK_ARGS=.*|HLBOT_TICK_ARGS=--live --execution maker|' /etc/hl-bot/env
sudo systemctl restart hlbot-tick.timer
```

From here it self-governs: the supervisor **auto-pauses** an agent that breaches its
loss guardrail and **demotes** on drawdown; the allocator sizes by recent
performance; trading halts entirely if capital drops to the floor (~$40). You get
Telegram alerts on trouble. **You don't touch it.**

---

## The off switch (one line, any time)
```bash
sudo sed -i 's|^HLBOT_TICK_ARGS=.*|HLBOT_TICK_ARGS=|' /etc/hl-bot/env   # back to paper
sudo systemctl restart hlbot-tick.timer
# freeze self-updates too:  sudo sed -i 's/^HLBOT_AUTO_UPDATE=.*/HLBOT_AUTO_UPDATE=0/' /etc/hl-bot/env
```

## What you must accept for true "set and forget"
- It **deploys its own code** changes (test-gated, never red; the loop is forbidden
  from enabling live or raising risk caps — those stay your decision via the env).
- Live trading risks real money, **bounded** by the daily-loss cap + capital floor.
- "Make money" is not guaranteed — if no edge is found, it trades tiny, loses
  within the guardrails, auto-pauses, and keeps trying. Check Telegram weekly.
