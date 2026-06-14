#!/usr/bin/env bash
# Auto-deploy: fast-forward the LIVE engine tree to reviewed `main` and restart
# when it advances. Runs as root (restarts services); git/uv as the hlbot user.
# Idempotent and SAFE: only deploys a CLEAN tree (never clobbers local/manual
# state), only fast-forwards. The kill switch and caps still bound the engine
# regardless of what's deployed.
set -euo pipefail
HOME_DIR="${HLBOT_HOME:-/opt/hl-bot}"
log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

git config --global --add safe.directory "$HOME_DIR" 2>/dev/null || true
sudo -u hlbot git -C "$HOME_DIR" fetch origin main -q
LOCAL="$(sudo -u hlbot git -C "$HOME_DIR" rev-parse HEAD)"
REMOTE="$(sudo -u hlbot git -C "$HOME_DIR" rev-parse origin/main)"
[ "$LOCAL" = "$REMOTE" ] && exit 0                       # nothing new
if [ -n "$(sudo -u hlbot git -C "$HOME_DIR" status --porcelain)" ]; then
  log "working tree dirty — skipping (manual/local state present)"; exit 0
fi
log "$LOCAL -> $REMOTE"
sudo -u hlbot git -C "$HOME_DIR" reset --hard origin/main
( cd "$HOME_DIR" && sudo -u hlbot uv sync --frozen )
systemctl restart hlbot-run hlbot-ws
log "live engine updated + restarted"
