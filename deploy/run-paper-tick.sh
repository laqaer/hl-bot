#!/usr/bin/env bash
# One paper forward-test cycle, invoked by hlbot-paper-tick.service. Ticks the
# FULL agent roster in paper mode against a dedicated paper DB, then evaluates
# goals/guardrails so the G1 audit trail accrues beside the paper book.
#
# This is the only place candidate (paper-mode) agents tick at all: the live
# tick filters them out, so without this loop NO G1 forward-test evidence
# accumulates anywhere (found Iter 85 — the live box had been live-ticking for
# days while the deploy DB held zero paper history; every promotion candidate's
# 30d calendar clock was simply not running).
set -uo pipefail
cd "${HLBOT_HOME:-/opt/hl-bot}" || exit 1

# Dedicated DB: paper evidence never mixes with the live book. Exported AFTER
# the unit's EnvironmentFile is read, so a stray HLBOT_DB in /etc/hl-bot/env
# can never point the paper loop at the live DB.
export HLBOT_DB="${HLBOT_PAPER_DB:-data/hlbot_paper.sqlite}"

run() { echo "[$(date -u +%H:%M:%S)] $*"; "$@" || echo "  (step failed: $*)"; }

# femr_tick WITHOUT --live (and deliberately ignoring HLBOT_TICK_ARGS, which
# carries the live box's "--live --execution maker"): logs decisions only,
# places no orders. No ingest step: the paper book is decision-replay only;
# pulling the live account's fills into the paper DB would blur the two
# evidence streams.
run uv run hlbot femr_tick
run uv run hlbot supervisor
