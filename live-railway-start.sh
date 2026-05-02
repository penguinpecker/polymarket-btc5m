#!/bin/sh
# Railway entrypoint for the LIVE service.
#
# Differs from railway-start.sh (paper) in three ways:
#   1. Volume mounted at /app/live, not /app/paper.
#   2. NO STATE_SEED_B64 — live state must NEVER be seeded; it's authored
#      from real on-chain reality. If the volume is empty, live_trade.py
#      starts at the canonical $100 paper-equivalent baseline.
#   3. Refuses to boot if both LIVE_ENABLED=true AND core secrets missing.
set -e

mkdir -p /app/live

if [ "$LIVE_ENABLED" = "true" ]; then
    if [ -z "$LIVE_PRIVATE_KEY" ]; then
        echo "[live] FATAL: LIVE_ENABLED=true but LIVE_PRIVATE_KEY unset" >&2
        exit 2
    fi
    echo "[live] LIVE_ENABLED=true — wallet/CLOB connectivity will be checked at python boot"
else
    echo "[live] LIVE_ENABLED=false — shadow mode (logs intended orders only)"
fi

exec python -u live_trade.py
