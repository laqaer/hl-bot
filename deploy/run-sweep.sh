#!/usr/bin/env bash
# Nightly research sweep: refresh history cache, run every sweep spec through
# the G0 gate, commit the ranked results so research sessions (Claude / ralph)
# always start from fresh evidence. Invoked by hlbot-sweep.timer (02:00 UTC).
set -euo pipefail
cd "${HLBOT_HOME:-/opt/hl-bot}"

UNIVERSE="${HLBOT_SWEEP_UNIVERSE:-BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE}"
DAYS="${HLBOT_SWEEP_DAYS:-180}"

echo "[sweep] refreshing ${DAYS}d history for ${UNIVERSE}"
uv run hlbot backtest-fetch --coins "$UNIVERSE" --interval 1h --days "$DAYS" --refresh || true

shopt -s nullglob
for spec in configs/sweeps/*.yaml; do
  echo "[sweep] $spec"
  uv run hlbot sweep "$spec" || echo "[sweep] $spec failed (continuing)"
done

# Commit results back (branch configured at deploy time; no-op when clean).
if [ -n "$(git status --porcelain research/results 2>/dev/null)" ]; then
  git add research/results
  git -c user.name="hlbot-sweep" -c user.email="hlbot@localhost" \
      commit -m "research: nightly sweep results $(date -u +%F)"
  git push origin "HEAD:${HLBOT_SWEEP_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}" \
    || echo "[sweep] push failed (configure deploy key / HLBOT_SWEEP_BRANCH)"
fi
