#!/bin/sh
# Push secrets from .env.live to the Railway live service via stdin.
# stdin avoids shell history; the file stays on local disk only.
set -e

ENV_FILE="$(dirname "$0")/.env.live"
SERVICE="polymarket-btc5m-live"

if [ ! -f "$ENV_FILE" ]; then
    echo "missing $ENV_FILE" >&2
    exit 1
fi

# shell-portable line read; ignore comments + blanks
while IFS='=' read -r key val; do
    case "$key" in
        ''|\#*) continue ;;
    esac
    if [ -z "$val" ]; then
        echo "skip $key (empty)"
        continue
    fi
    printf '%s' "$val" | railway variable --service "$SERVICE" --skip-deploys --set-from-stdin "$key" >/dev/null
    echo "set  $key (len=${#val})"
done < "$ENV_FILE"

echo
echo "Done. Verify with:"
echo "  railway variable --service $SERVICE --kv | grep -E '^LIVE_(PRIVATE|CLOB)'"
