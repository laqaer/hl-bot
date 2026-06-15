#!/usr/bin/env bash
# Nightly FORWARD auto-confirm (P1c). After the sweep refreshes history, re-run
# the G0 gate over the accrued forward window for every unconfirmed paper agent
# and stamp the verdict (+ params_hash, V3) into the confirmations table. The
# supervisor (hlbot run, every supervise_every_s) then auto-promotes any agent
# that now clears a params-matched G0 — no human step. Invoked by
# hlbot-confirm.timer (03:00 UTC, after hlbot-sweep at 02:00).
#
# Confirmations live in the DB (Litestream-replicated), so unlike the sweep this
# writes NO git artifacts — there is nothing to commit.
set -euo pipefail
cd "${HLBOT_HOME:-/opt/hl-bot}"

# Universe matches the sweep breadth (dislocation/crowding-fade need it for
# enough events). --days 0 lets autoconfirm pick an HL-retention-aware window
# per agent interval (5m→90 req ≈ ~17.5d real; 1h→210 ≈ ~190d real).
UNIVERSE="${HLBOT_CONFIRM_UNIVERSE:-${HLBOT_SWEEP_UNIVERSE:-BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE}}"

echo "[confirm] autoconfirm over forward window for ${UNIVERSE}"
uv run hlbot autoconfirm --coins "$UNIVERSE" --record
