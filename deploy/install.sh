#!/usr/bin/env bash
# One-command deploy for hl-bot on a fresh Ubuntu/Debian host. Idempotent: safe to
# re-run to update. Installs as a locked-down system user under systemd timers.
#
#   sudo REPO_URL=https://github.com/<you>/hl-bot.git BRANCH=main bash deploy/install.sh
#
# Defaults to PAPER trading. Going live is a deliberate, separate step:
#   1) put an API wallet at ~hlbot/.config/hermes/hl-bot-api-wallet.env (chmod 600)
#   2) confirm a strategy: `sudo -u hlbot uv run hlbot confirm --agent <a> --prefer maker`
#   3) enable it: agent_state -> live_small (see docs/GO_LIVE.md)
#   4) set HLBOT_TICK_ARGS="--live --execution maker" in /etc/hl-bot/env
set -euo pipefail

HLBOT_USER="${HLBOT_USER:-hlbot}"
HLBOT_HOME="${HLBOT_HOME:-/opt/hl-bot}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"
ENV_DIR="/etc/hl-bot"
ENV_FILE="${ENV_DIR}/env"

log() { printf '\n\033[1;36m[install]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[install] %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

log "1/8 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates >/dev/null

log "2/8 service user ${HLBOT_USER}"
id -u "$HLBOT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$HLBOT_USER"

log "3/8 uv (Python toolchain)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
command -v uv >/dev/null 2>&1 || die "uv install failed"

log "4/8 fetch repo into ${HLBOT_HOME} (branch ${BRANCH})"
if [ -d "${HLBOT_HOME}/.git" ]; then
  git -C "$HLBOT_HOME" fetch --depth 1 origin "$BRANCH"
  git -C "$HLBOT_HOME" checkout -q "$BRANCH"
  git -C "$HLBOT_HOME" reset --hard "origin/${BRANCH}"
else
  [ -n "$REPO_URL" ] || die "REPO_URL required for first install"
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$HLBOT_HOME"
fi
mkdir -p "${HLBOT_HOME}/data"
chown -R "$HLBOT_USER":"$HLBOT_USER" "$HLBOT_HOME"
chmod +x "${HLBOT_HOME}"/deploy/*.sh "${HLBOT_HOME}"/ralph/*.sh 2>/dev/null || true

log "5/8 python deps (uv sync)"
sudo -u "$HLBOT_USER" sh -c "cd '$HLBOT_HOME' && uv sync --frozen >/dev/null"

log "6/8 environment file ${ENV_FILE}"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cp "${HLBOT_HOME}/deploy/env.example" "$ENV_FILE"
  echo "HLBOT_HOME=${HLBOT_HOME}" >> "$ENV_FILE"
  log "  -> created ${ENV_FILE} from template; EDIT IT (HL_ADDRESS, alerts)"
fi
chmod 640 "$ENV_FILE"; chown root:"$HLBOT_USER" "$ENV_FILE"   # bot user can read; others can't

log "7/8 systemd units"
for f in "${HLBOT_HOME}"/deploy/systemd/*; do
  install -m 644 <(sed -e "s#/opt/hl-bot#${HLBOT_HOME}#g" -e "s#User=hlbot#User=${HLBOT_USER}#g" "$f") \
    "/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload
systemctl enable --now hlbot-tick.timer hlbot-report.timer hlbot-ws.service
log "  -> tick + report timers + ws feed enabled (PAPER). hlbot-loop NOT enabled (start manually)."

log "8/8 optional Litestream backups"
# shellcheck disable=SC1090
. "$ENV_FILE" 2>/dev/null || true
if [ -n "${LITESTREAM_S3_BUCKET:-}" ]; then
  arch="$(uname -m)"; ls_arch="amd64"; [ "$arch" = "aarch64" ] && ls_arch="arm64"
  if ! command -v litestream >/dev/null 2>&1; then
    curl -LsS "https://github.com/benbjohnson/litestream/releases/latest/download/litestream-linux-${ls_arch}.tar.gz" \
      | tar -xz -C /usr/local/bin litestream || log "  litestream install skipped (fetch failed)"
  fi
  # Render concrete config (don't rely on env-expansion); creds come from the
  # IAM instance role on EC2 (no static keys), region from AWS_REGION in the env.
  sed -e "s|\${LITESTREAM_S3_BUCKET}|${LITESTREAM_S3_BUCKET}|g" \
      -e "s|\${LITESTREAM_S3_PATH}|${LITESTREAM_S3_PATH:-hl-bot/hlbot.sqlite}|g" \
      "${HLBOT_HOME}/deploy/litestream.yml" > /etc/litestream.yml
  mkdir -p /etc/systemd/system/litestream.service.d
  printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" > /etc/systemd/system/litestream.service.d/env.conf
  systemctl daemon-reload
  systemctl enable --now litestream 2>/dev/null || log "  start litestream after IAM/creds are in place"
  log "  -> Litestream replicating ${HLBOT_HOME}/data/hlbot.sqlite to s3://${LITESTREAM_S3_BUCKET}"
else
  log "  LITESTREAM_S3_BUCKET unset -> skipping backups (set it in ${ENV_FILE} to enable)"
fi

log "preflight"
sudo -u "$HLBOT_USER" sh -c "cd '$HLBOT_HOME' && set -a && . '$ENV_FILE' && uv run hlbot doctor" || true

cat <<EOF

✅ hl-bot installed under ${HLBOT_HOME} as user ${HLBOT_USER} (PAPER mode).
   Edit ${ENV_FILE}, then:  systemctl restart hlbot-tick.timer
   Status:   systemctl list-timers 'hlbot-*' ; journalctl -u hlbot-tick -f
   Go live:  see docs/GO_LIVE.md (deliberate, gated).
EOF
