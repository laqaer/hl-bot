#!/usr/bin/env bash
# One scheduling cycle, invoked by hlbot-tick.service. Ingest fresh exchange
# truth, run the agents (PAPER unless HLBOT_TICK_ARGS opts into --live), evaluate
# guardrails, then self-assess health. Each step is best-effort so one transient
# failure doesn't skip the rest; health runs last and pages on trouble.
set -uo pipefail
cd "${HLBOT_HOME:-/opt/hl-bot}" || exit 1

run() { echo "[$(date -u +%H:%M:%S)] $*"; "$@" || echo "  (step failed: $*)"; }

run uv run hlbot ingest
# HLBOT_TICK_ARGS is intentionally unquoted so "" => paper, or "--live --execution maker".
# shellcheck disable=SC2086
run uv run hlbot femr_tick ${HLBOT_TICK_ARGS:-}
run uv run hlbot supervisor
run uv run hlbot health
