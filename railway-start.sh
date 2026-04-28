#!/bin/sh
# Railway entrypoint — seeds /app/paper from STATE_SEED_B64 (gz tarball, base64)
# on first boot only, then exec's the bot. Volume marker (.seeded) prevents re-seed.
set -e

mkdir -p /app/paper

if [ -n "$STATE_SEED_B64" ] && [ ! -f /app/paper/.seeded ]; then
    echo "[seed] migrating state from STATE_SEED_B64 ($(echo "$STATE_SEED_B64" | wc -c) bytes b64)"
    echo "$STATE_SEED_B64" | base64 -d | tar xz -C /app/paper
    touch /app/paper/.seeded
    echo "[seed] migrated:"
    ls -la /app/paper/
fi

exec python -u paper_trade.py
