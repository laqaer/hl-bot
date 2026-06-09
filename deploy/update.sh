#!/usr/bin/env bash
# Auto-deploy: pick up improvements the Ralph loop (or you) pushed to the branch,
# TEST-GATE them, and restart the bot only if green — so a self-improving bot can
# ship its own changes without ever deploying red code. Runs as root (to restart
# units); all repo/test work runs as the hlbot user. Gated by HLBOT_AUTO_UPDATE=1.
set -uo pipefail

ENV_FILE=/etc/hl-bot/env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
[ "${HLBOT_AUTO_UPDATE:-0}" = "1" ] || { echo "auto-update disabled (HLBOT_AUTO_UPDATE!=1)"; exit 0; }

HOME_DIR="${HLBOT_HOME:-/opt/hl-bot}"
USER_="${HLBOT_USER:-hlbot}"
as_hlbot() { sudo -u "$USER_" bash -lc "cd '$HOME_DIR' && $*"; }

BR="$(as_hlbot 'git rev-parse --abbrev-ref HEAD')" || exit 0
as_hlbot "git fetch origin '$BR' -q" || true
as_hlbot "git merge --ff-only 'origin/$BR' -q" >/dev/null 2>&1 || true

current="$(as_hlbot 'git rev-parse HEAD')"
deployed="$(cat "$HOME_DIR/data/.deployed_sha" 2>/dev/null || echo none)"
[ "$current" = "$deployed" ] && exit 0

echo "[update] candidate $deployed -> $current"
as_hlbot 'uv sync --frozen -q' || { echo "[update] uv sync failed; aborting"; exit 1; }
if as_hlbot 'uv run pytest -q' >/tmp/hlbot_update.log 2>&1; then
  echo "$current" > "$HOME_DIR/data/.deployed_sha"
  chown "$USER_":"$USER_" "$HOME_DIR/data/.deployed_sha" 2>/dev/null || true
  systemctl restart hlbot-tick.timer hlbot-ws.service hlbot-recorder.service 2>/dev/null || true
  echo "[update] deployed + restarted $current"
else
  echo "[update] tests RED at $current — NOT deploying (frozen at $deployed)"
  tail -5 /tmp/hlbot_update.log || true
fi
