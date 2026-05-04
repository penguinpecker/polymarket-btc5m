#!/bin/sh
# Railway entrypoint — dispatches to live trader or claim sweeper based on
# ROLE env var.
set -e

if [ "$ROLE" = "live" ]; then
    exec /app/live-railway-start.sh
fi

if [ "$ROLE" = "sweeper" ]; then
    echo "[sweeper] starting claim sweeper — redeems winning+losing tokens via Safe execTransaction, wraps USDC.e -> pUSD, retries forever"
    exec python -u claim_sweeper.py
fi

echo "[entrypoint] FATAL: unknown ROLE='$ROLE' — set ROLE=live or ROLE=sweeper" >&2
exit 2
