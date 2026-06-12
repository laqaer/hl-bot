#!/usr/bin/env bash
# Ralph loop: run the standing prompt repeatedly so the agent makes one tested
# increment of progress per iteration. Self-improvement / research only — it
# never enables live trading (the prompt forbids it and this script grants no
# live/network capability beyond what the agent already has).
#
# Usage:
#   ralph/loop.sh                 # 25 iterations, commit on green, no push
#   RALPH_ITERS=50 ralph/loop.sh  # more iterations
#   RALPH_PUSH=1 ralph/loop.sh    # also push to the current branch each green commit
#   touch ralph/STOP              # graceful stop after the current iteration
#
# Env:
#   RALPH_ITERS   max iterations            (default 25)
#   RALPH_PUSH    push after green commit    (default 0)
#   CLAUDE_BIN    claude executable          (default: claude)
#   CLAUDE_MODEL  model                      (default: claude-opus-4-8)
#   CLAUDE_FLAGS  extra flags for `claude -p` (default: a non-interactive set)
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

ITERS="${RALPH_ITERS:-25}"
PUSH="${RALPH_PUSH:-0}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-fable-5}"
# Non-interactive by default. Review before granting broader autonomy.
CLAUDE_FLAGS="${CLAUDE_FLAGS:---permission-mode acceptEdits}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PROMPT_FILE="$ROOT/ralph/PROMPT.md"

# Use the Claude subscription (OAuth) instead of a metered API key: strip API-key
# env vars so Claude Code falls back to its stored OAuth login. Set CLAUDE_USE_OAUTH=1.
if [ "${CLAUDE_USE_OAUTH:-0}" = "1" ]; then
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
fi

log() { printf '\n\033[1;36m[ralph %s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }

verify() {
  uv run pytest -q >/tmp/ralph_pytest.log 2>&1 || { log "TESTS RED"; tail -20 /tmp/ralph_pytest.log; return 1; }
  uv run ruff check src tests scripts >/tmp/ralph_ruff.log 2>&1 || { log "RUFF RED"; tail -20 /tmp/ralph_ruff.log; return 1; }
  return 0
}

command -v "$CLAUDE_BIN" >/dev/null 2>&1 || { log "claude CLI not found ($CLAUDE_BIN); install it or set CLAUDE_BIN"; exit 1; }

log "branch=$BRANCH iters=$ITERS push=$PUSH model=$CLAUDE_MODEL"
log "baseline verify…"; verify || { log "baseline is already red — fix before looping"; exit 1; }

for i in $(seq 1 "$ITERS"); do
  [ -f "$ROOT/ralph/STOP" ] && { log "STOP file present — exiting"; rm -f "$ROOT/ralph/STOP"; break; }
  log "iteration $i/$ITERS"

  # Keep the rolling candle store fresh. The systemd harvest timer can't run on
  # this box (no root), and 1m API retention is only ~3.5d — letting more than
  # that pass between harvests gaps the store and silently invalidates B-G014's
  # multi-week sample. Best-effort: a network blip must not block the iteration.
  uv run hlbot harvest-candles >/tmp/ralph_harvest.log 2>&1 \
    && log "candle store topped up" \
    || log "harvest-candles failed (continuing; see /tmp/ralph_harvest.log)"

  before="$(git rev-parse HEAD)"
  "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" --model "$CLAUDE_MODEL" $CLAUDE_FLAGS \
    || log "claude exited non-zero (continuing)"

  if ! verify; then
    log "post-iteration verify failed; reverting working tree to last commit"
    git reset --hard "$before" >/dev/null 2>&1
    git clean -fd src tests scripts ralph docs >/dev/null 2>&1
    continue
  fi

  after="$(git rev-parse HEAD)"
  if [ "$before" = "$after" ]; then
    # Agent made changes but didn't commit; commit them on its behalf (green).
    if ! git diff --quiet || ! git diff --cached --quiet; then
      git add -A && git commit -q -m "ralph: iteration $i" && after="$(git rev-parse HEAD)"
      log "committed uncommitted green changes"
    else
      log "no changes this iteration"
    fi
  fi

  if [ "$PUSH" = "1" ] && [ "$before" != "$after" ]; then
    # The branch is shared (a human pushes here too), so an advanced remote would
    # reject a plain push. Rebase our new commit(s) onto origin first, then push.
    git fetch origin "$BRANCH" -q 2>/dev/null
    if git rebase "origin/$BRANCH" >/dev/null 2>&1; then
      git push -u origin "$BRANCH" >/dev/null 2>&1 && log "pushed to $BRANCH" || log "push failed (will retry next green)"
    else
      git rebase --abort >/dev/null 2>&1
      log "rebase onto origin/$BRANCH conflicted; keeping local commit, will retry next green"
    fi
  fi
done

log "done"
