#!/bin/sh
# Railway entrypoint for the LIVE service.
#
# Volume mounted at /app/live. Refuses to boot if LIVE_ENABLED=true but
# core secrets are missing. Bankroll is reconciled to on-chain state at
# boot (see live_trade.reconcile_with_chain).
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
