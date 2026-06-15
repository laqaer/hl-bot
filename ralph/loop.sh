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
#   RALPH_ITERS    max iterations                       (default 25)
#   RALPH_PUSH     push after green commit               (default 0)
#   RALPH_TIMEOUT  per-iteration hard cap, seconds       (default 1800)
#   RALPH_MAX_FAILS abort after N consecutive bad iters  (default 5)
#   RALPH_SKIP_AUTH_CHECK  skip the startup auth probe    (default 0)
#   CLAUDE_BIN     claude executable                     (default: claude)
#   CLAUDE_MODEL   model                                 (default: claude-opus-4-8)
#   CLAUDE_FLAGS   extra flags for `claude -p`           (default: a non-interactive set)
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

ITERS="${RALPH_ITERS:-25}"
PUSH="${RALPH_PUSH:-0}"
TIMEOUT="${RALPH_TIMEOUT:-1800}"
MAX_FAILS="${RALPH_MAX_FAILS:-5}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"
# Non-interactive by default. Review before granting broader autonomy.
CLAUDE_FLAGS="${CLAUDE_FLAGS:---permission-mode acceptEdits}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
# A systemd service user usually has no git identity -> commits fail with
# "empty ident name". Set a repo-local one if missing so the loop can commit.
git config user.email >/dev/null 2>&1 || git config user.email "ralph@hl-bot.local"
git config user.name  >/dev/null 2>&1 || git config user.name  "hlbot-ralph"
# Per-process temp (was hardcoded /tmp/ralph_* — collided across users/clones,
# making verify() report a false RED when another user owned the file).
RALPH_TMP="$(mktemp -d -t ralph.XXXXXX)"
trap 'rm -rf "$RALPH_TMP"' EXIT
PROMPT_FILE="$ROOT/ralph/PROMPT.md"

log() { printf '\n\033[1;36m[ralph %s]\033[0m %s\n' "$(date -u +%H:%M:%S)" "$*"; }

verify() {
  uv run pytest -q >"$RALPH_TMP/pytest.log" 2>&1 || { log "TESTS RED"; tail -20 "$RALPH_TMP/pytest.log"; return 1; }
  uv run ruff check src tests scripts >"$RALPH_TMP/ruff.log" 2>&1 || { log "RUFF RED"; tail -20 "$RALPH_TMP/ruff.log"; return 1; }
  return 0
}

# Run claude under a hard wall-clock cap so a hung session can't wedge the loop.
# `timeout` is optional (not on every minimal host); fall back to a bare call.
run_agent() {
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 30s "$TIMEOUT" "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
      --model "$CLAUDE_MODEL" $CLAUDE_FLAGS
  else
    "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" --model "$CLAUDE_MODEL" $CLAUDE_FLAGS
  fi
}

# Push with bounded exponential backoff (2s,4s,8s,16s) — matches the operator
# git policy and survives transient network blips on a long unattended run.
push_branch() {
  local delay=2 try
  for try in 1 2 3 4; do
    if git push -u origin "$BRANCH" >/dev/null 2>&1; then
      log "pushed to $BRANCH"; return 0
    fi
    log "push failed (attempt $try) — retrying in ${delay}s"
    sleep "$delay"; delay=$((delay * 2))
  done
  log "push still failing after retries; will retry next green commit"
  return 1
}

command -v "$CLAUDE_BIN" >/dev/null 2>&1 || { log "claude CLI not found ($CLAUDE_BIN); install it or set CLAUDE_BIN"; exit 1; }

# Auth liveness probe: fail fast with a clear message instead of silently
# no-op'ing every iteration when the OAuth/Max session (or ANTHROPIC_API_KEY)
# is missing or expired — the most common unattended-run failure.
if [ "${RALPH_SKIP_AUTH_CHECK:-0}" != "1" ]; then
  log "auth probe…"
  if ! timeout 90s "$CLAUDE_BIN" -p "reply with the single word READY" \
        --model "$CLAUDE_MODEL" $CLAUDE_FLAGS >"$RALPH_TMP/auth.log" 2>&1 \
        || ! grep -qi "READY" "$RALPH_TMP/auth.log"; then
    log "auth probe FAILED — is the claude CLI authenticated? (run 'claude' once"
    log "to sign in via OAuth/Max, or export ANTHROPIC_API_KEY). Set"
    log "RALPH_SKIP_AUTH_CHECK=1 to bypass. Last output:"; tail -5 "$RALPH_TMP/auth.log"
    exit 1
  fi
fi

log "branch=$BRANCH iters=$ITERS push=$PUSH model=$CLAUDE_MODEL timeout=${TIMEOUT}s"
log "baseline verify…"; verify || { log "baseline is already red — fix before looping"; exit 1; }

fails=0
for i in $(seq 1 "$ITERS"); do
  # Exit 42 (not 0) so the graceful STOP is honored under Restart=always:
  # the unit sets RestartPreventExitStatus=42, so systemd will NOT relaunch us.
  [ -f "$ROOT/ralph/STOP" ] && { log "STOP file present — exiting"; rm -f "$ROOT/ralph/STOP"; exit 42; }
  log "iteration $i/$ITERS"

  before="$(git rev-parse HEAD)"
  rc=0; run_agent || rc=$?
  if [ "$rc" -ne 0 ]; then
    [ "$rc" = 124 ] && log "claude TIMED OUT after ${TIMEOUT}s" || log "claude exited non-zero (rc=$rc)"
  fi

  if ! verify; then
    log "post-iteration verify failed; reverting working tree to last commit"
    git reset --hard "$before" >/dev/null 2>&1
    # Full clean (no -x: gitignored data/ + *.sqlite stay) so a red iteration
    # can't strand untracked files that poison the next verify.
    git clean -fd >/dev/null 2>&1
    fails=$((fails + 1))
    [ "$fails" -ge "$MAX_FAILS" ] && { log "aborting: $fails consecutive failed iterations (RALPH_MAX_FAILS)"; exit 1; }
    continue
  fi

  after="$(git rev-parse HEAD)"
  if [ "$before" = "$after" ]; then
    # Agent made changes but didn't commit; commit them on its behalf (green).
    if ! git diff --quiet || ! git diff --cached --quiet; then
      if git add -A && git commit -q -m "ralph: iteration $i"; then
        after="$(git rev-parse HEAD)"; log "committed uncommitted green changes"
      else
        log "COMMIT FAILED (check git identity / state) — work not saved"; continue
      fi
    else
      log "no changes this iteration"
      fails=$((fails + 1))
      [ "$fails" -ge "$MAX_FAILS" ] && { log "aborting: $fails consecutive no-op/failed iterations (RALPH_MAX_FAILS)"; exit 1; }
      continue
    fi
  fi

  fails=0  # a real green commit resets the circuit breaker
  if [ "$PUSH" = "1" ] && [ "$before" != "$after" ]; then
    push_branch
  fi
done

log "done"
