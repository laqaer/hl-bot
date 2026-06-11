#!/usr/bin/env bash
# Set up the self-improvement loop on an always-on box (your AWS instance or any
# VPS). It runs in a SEPARATE clone (/opt/hl-bot-loop) so the autonomous agent
# never touches the live trading dir — the live box only auto-deploys clean,
# test-passed commits the loop pushes to GitHub.
#
#   sudo GITHUB_TOKEN=ghp_... \
#        REPO_URL=https://github.com/laqaer/hl-bot.git \
#        BRANCH=claude/gracious-fermat-g1QZ4 \
#        bash deploy/setup-loop.sh
#
# GITHUB_TOKEN: a fine-grained PAT with Contents: Read+Write on the repo (so the
# loop can PUSH its improvements). Without push, the loop's work never reaches the
# live bot. The token is stored only in the loop clone's git remote (chmod 700 dir).
set -euo pipefail

HLBOT_USER="${HLBOT_USER:-hlbot}"
LOOP_HOME="${LOOP_HOME:-/opt/hl-bot-loop}"
REPO_URL="${REPO_URL:-https://github.com/laqaer/hl-bot.git}"
BRANCH="${BRANCH:-claude/gracious-fermat-g1QZ4}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

log() { printf '\n\033[1;36m[setup-loop]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[setup-loop] %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
id -u "$HLBOT_USER" >/dev/null 2>&1 || die "user $HLBOT_USER missing — run deploy/install.sh first"
[ -n "$GITHUB_TOKEN" ] || die "GITHUB_TOKEN required (fine-grained PAT, Contents:Read+Write) so the loop can push"

# Build an authenticated push URL (token embedded; kept in the private loop dir only).
AUTH_URL="$(printf '%s' "$REPO_URL" | sed -E "s#https://#https://x-access-token:${GITHUB_TOKEN}@#")"

log "1/5 clone loop workspace at ${LOOP_HOME} (branch ${BRANCH})"
if [ -d "${LOOP_HOME}/.git" ]; then
  sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" remote set-url origin "$AUTH_URL"
  sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" fetch --depth 1 origin "$BRANCH"
  sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" checkout -B "$BRANCH" "origin/${BRANCH}"
else
  install -d -o "$HLBOT_USER" -g "$HLBOT_USER" -m 700 "$LOOP_HOME"
  sudo -u "$HLBOT_USER" git clone --depth 1 -b "$BRANCH" "$AUTH_URL" "$LOOP_HOME"
fi
chmod 700 "$LOOP_HOME"   # token is in .git/config — keep the dir private
sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" config core.fileMode false
sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" config user.email "loop@hl-bot.local"
sudo -u "$HLBOT_USER" git -C "$LOOP_HOME" config user.name "hl-bot loop"

log "2/5 toolchain (uv + Claude Code CLI) for ${HLBOT_USER}"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
sudo -u "$HLBOT_USER" -H bash -lc 'command -v claude >/dev/null 2>&1 || curl -fsSL https://claude.ai/install.sh | bash'

log "3/5 sync python deps"
sudo -u "$HLBOT_USER" bash -lc "cd '$LOOP_HOME' && uv sync --frozen >/dev/null"

log "4/5 install systemd unit"
sed -e "s#/opt/hl-bot-loop#${LOOP_HOME}#g" -e "s#User=hlbot#User=${HLBOT_USER}#g" \
    -e "s#/home/hlbot#/home/${HLBOT_USER}#g" \
    "$(dirname "$0")/systemd/hlbot-loop.service" > /etc/systemd/system/hlbot-loop.service
systemctl daemon-reload

log "5/5 baseline check"
sudo -u "$HLBOT_USER" bash -lc "cd '$LOOP_HOME' && uv run pytest -q >/dev/null 2>&1" \
  && log "  tests green" || log "  WARNING: baseline tests not green; the loop will refuse to start until fixed"

CLAUDE_BIN="/home/${HLBOT_USER}/.local/bin/claude"
cat <<EOF

✅ Loop workspace ready at ${LOOP_HOME} (pushes to ${BRANCH}).

TWO manual steps remain (one-time):

  1. Get a long-lived OAuth token from your Claude subscription (no API billing).
     NOTE: setup-token is NOT an interactive login — it PRINTS a token (shown
     ONCE) that you save yourself into the loop's env so it runs headless.
       sudo -u ${HLBOT_USER} -H ${CLAUDE_BIN} setup-token
     Open the printed URL, authorize, COPY the token, then store it where the loop
     reads it (the paste is hidden — not echoed, not in shell history):
       sudo bash -c 'umask 077; read -rsp "Paste token, then Enter: " T && printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "\$T" > /etc/hl-bot/loop.env && chmod 600 /etc/hl-bot/loop.env'
     Verify:
       sudo bash -c 'set -a; . /etc/hl-bot/loop.env; set +a; sudo -u ${HLBOT_USER} -H --preserve-env=CLAUDE_CODE_OAUTH_TOKEN ${CLAUDE_BIN} -p "reply OK"'

  2. Start it (and on every boot):
       sudo systemctl enable --now hlbot-loop
       journalctl -u hlbot-loop -f      # watch it work (look for "iteration 1")

The loop now runs 24/7 here, pushing improvements that the live bot auto-deploys.
It can never enable live trading or raise risk caps (ralph/PROMPT.md).
EOF
