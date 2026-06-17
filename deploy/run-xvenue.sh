#!/usr/bin/env bash
# Hourly cross-venue funding accrual (S5 fuel). Funding rates on Binance/Bybit
# change only every 8h, but fetching hourly keeps the xvenue_funding table fresh
# for any S5 filter and aligns the timestamp with the live cycle. Invoked by
# hlbot-xvenue.timer.
set -euo pipefail
cd "${HLBOT_HOME:-/opt/hl-bot}"

UNIVERSE="${HLBOT_XVENUE_COINS:-${HLBOT_CONFIRM_UNIVERSE:-${HLBOT_SWEEP_UNIVERSE:-BTC,ETH,SOL,HYPE,DOGE,XRP,WIF,kPEPE}}}}"

echo "[xvenue] fetching cross-venue funding for ${UNIVERSE}"
uv run hlbot accrue-xvenue --coins "$UNIVERSE"
