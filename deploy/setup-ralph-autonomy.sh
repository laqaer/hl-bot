#!/usr/bin/env bash
# Stand up the Ralph self-improvement loop in a SEPARATE clone so it never runs
# inside the LIVE engine's working tree. The live bot (hlbot-run, /opt/hl-bot)
# keeps running reviewed `main`; Ralph experiments in /opt/hl-bot-ralph on a dev
# branch and pushes green commits there — its work reaches the live bot only via
# reviewed PRs merged to main + a deliberate `git pull`, NOT by editing the live
# tree under it. This is the privilege-separation the audit flagged (V5).
#
# Prerequisites (checked below; warnings, not hard fails):
#   1. `claude` CLI installed and authed FOR THE hlbot user — put a Max/Pro
#      token from `claude setup-token` into /etc/hl-bot/env as
#      CLAUDE_CODE_OAUTH_TOKEN (do NOT set ANTHROPIC_API_KEY; it bills per-call).
#   2. git push creds for the hlbot user (deploy key or token with write access)
#      if you want RALPH_PUSH=1 to publish its work. Without them the loop still
#      runs and commits locally; it just can't push.
#
# Usage (run as a sudo-capable user):
#   sudo bash deploy/setup-ralph-autonomy.sh
#   sudo systemctl enable --now hlbot-ralph
#   sudo journalctl -u hlbot-ralph -f
#
# Refresh the clone to latest main after its PRs merge:
#   sudo bash deploy/setup-ralph-autonomy.sh     # re-running resets to origin/main
set -euo pipefail

LIVE_HOME="${HLBOT_HOME:-/opt/hl-bot}"
RALPH_HOME="${RALPH_HOME:-/opt/hl-bot-ralph}"
BRANCH="${RALPH_BRANCH:-claude/ralph-auto}"
log() { printf '[setup-ralph] %s\n' "$*"; }

[ -d "$LIVE_HOME/.git" ] || { log "FATAL: $LIVE_HOME is not a git checkout"; exit 1; }
log "live=$LIVE_HOME  ralph-clone=$RALPH_HOME  branch=$BRANCH"

# 1. separate clone owned by hlbot, on a dev branch tracking origin/main
ORIGIN="$(sudo -u hlbot git -C "$LIVE_HOME" remote get-url origin)"
sudo -u hlbot git config --global --add safe.directory "$RALPH_HOME" 2>/dev/null || true
if [ ! -d "$RALPH_HOME/.git" ]; then
  log "cloning $ORIGIN -> $RALPH_HOME"
  sudo -u hlbot git clone "$ORIGIN" "$RALPH_HOME"
fi
sudo -u hlbot git -C "$RALPH_HOME" fetch origin
sudo -u hlbot git -C "$RALPH_HOME" checkout -B "$BRANCH" origin/main
sudo -u hlbot git -C "$RALPH_HOME" config pull.ff only

# 2. deps in the clone (own venv; never shares the live tree's)
( cd "$RALPH_HOME" && sudo -u hlbot uv sync --frozen )

# 3. prerequisite checks (warn only)
sudo -u hlbot bash -lc 'command -v claude >/dev/null 2>&1' \
  || log "WARN: claude CLI not found for hlbot — install + auth it before enabling the loop"
sudo grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' /etc/hl-bot/env 2>/dev/null \
  || log "WARN: CLAUDE_CODE_OAUTH_TOKEN missing in /etc/hl-bot/env — the loop fails its auth probe"
if ! sudo -u hlbot git -C "$RALPH_HOME" ls-remote --exit-code origin >/dev/null 2>&1; then
  log "WARN: hlbot may lack push creds to origin — RALPH_PUSH=1 will commit locally but not publish"
fi

# 4. install the unit; ensure the live-tree loop is OFF so we never run both
RHOME_ESC="$(printf '%s' "$RALPH_HOME" | sed 's/[&/]/\\&/g')"
sed "s|/opt/hl-bot-ralph|$RHOME_ESC|g" "$LIVE_HOME/deploy/systemd/hlbot-ralph.service" \
  > /etc/systemd/system/hlbot-ralph.service
systemctl disable --now hlbot-loop 2>/dev/null || true   # the live-tree loop, if it was on
systemctl daemon-reload
log "done. The live engine ($LIVE_HOME) is untouched and on main."
log "start the loop:  sudo systemctl enable --now hlbot-ralph"
log "watch it:        sudo journalctl -u hlbot-ralph -f"
