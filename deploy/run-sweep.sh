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

# Publish results to a DEDICATED results branch WITHOUT advancing the deployed
# branch's HEAD — committing onto the deployment branch (and failing to push)
# strands a divergent local commit that blocks every future `git pull`. We
# build the commit object with `commit-tree` (HEAD never moves) and push only
# that ref; the working tree is then reverted so the deployment stays pristine.
if [ -n "$(git status --porcelain research/results 2>/dev/null)" ]; then
  results_branch="${HLBOT_SWEEP_BRANCH:-sweep-results}"
  git add research/results
  tree="$(git write-tree)"
  parent="$(git rev-parse HEAD)"
  commit="$(git -c user.name='hlbot-sweep' -c user.email='hlbot@localhost' \
    commit-tree "$tree" -p "$parent" -m "research: nightly sweep results $(date -u +%F)")"
  git push origin "$commit:refs/heads/$results_branch" \
    || echo "[sweep] results push failed (configure deploy key); results stay local only"
  # Leave the deployment branch exactly as it was — HEAD never moved, so unstage
  # and discard the result files from the working tree (they live on the results
  # branch now). `git pull` on this box always fast-forwards.
  git restore --staged research/results 2>/dev/null || git reset -q HEAD research/results
  git checkout -- research/results 2>/dev/null || true
  git clean -fdq research/results 2>/dev/null || true
fi

