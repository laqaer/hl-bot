#!/usr/bin/env bash
# Ensure the host has swap. A small box (e.g. 2GB t4g.small) running the trader +
# WS feed — plus uv builds and optionally the self-improvement loop — can exhaust
# RAM. With NO swap the kernel OOM-kills processes and sshd can't fork a login
# shell, so SSH "accepts then resets": you see
#   kex_exchange_identification: read: Connection reset by peer
# even though port 22 is open. Swap gives headroom so transient pressure pages out
# instead of killing, keeping sshd reachable and the trader alive.
#
# Idempotent and standalone — the fastest durable fix once you can get a shell:
#   sudo bash deploy/ensure-swap.sh        # default 4G; override with SWAP_GB=2
set -euo pipefail

SWAP_GB="${SWAP_GB:-4}"
SWAPFILE="${SWAPFILE:-/swapfile}"
[ "$(id -u)" -eq 0 ] || { echo "[swap] run as root (sudo)"; exit 1; }

if swapon --show=NAME --noheadings 2>/dev/null | grep -q .; then
  echo "[swap] already active:"; swapon --show
else
  echo "[swap] creating ${SWAP_GB}G at ${SWAPFILE}"
  rm -f "$SWAPFILE" 2>/dev/null || true
  # fallocate is fast but produces a sparse file mkswap may reject on some FS;
  # fall back to dd (slower, fully allocated) if it does.
  if ! fallocate -l "${SWAP_GB}G" "$SWAPFILE" 2>/dev/null \
     || ! mkswap "$SWAPFILE" >/dev/null 2>&1; then
    echo "[swap] fallocate path failed; using dd"
    dd if=/dev/zero of="$SWAPFILE" bs=1M count="$((SWAP_GB * 1024))" status=none
    mkswap "$SWAPFILE" >/dev/null
  fi
  chmod 600 "$SWAPFILE"
  swapon "$SWAPFILE"
  grep -q "^${SWAPFILE} " /etc/fstab 2>/dev/null \
    || echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
  echo "[swap] active:"; swapon --show
fi

# Prefer RAM; use swap as a safety valve, not aggressively. Persist the setting.
sysctl -wq vm.swappiness=10 2>/dev/null || true
if ! grep -qs '^vm.swappiness' /etc/sysctl.d/99-hlbot.conf 2>/dev/null; then
  echo 'vm.swappiness=10' >> /etc/sysctl.d/99-hlbot.conf
fi
