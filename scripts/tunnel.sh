#!/usr/bin/env bash
# Prints the public URL and checks the tunnel reaches both consumers.
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE=$( [ -f .env ] && echo .env || echo env )
DOMAIN=$(grep -E '^NGROK_DOMAIN=' "$ENVFILE" | sed 's/[[:space:]]*#.*//' | cut -d= -f2)
echo "public: https://$DOMAIN"
echo -n "service /health via tunnel: "; curl -s -o /dev/null -w "%{http_code}\n" -H "ngrok-skip-browser-warning: 1" "https://$DOMAIN/health" || true
echo -n "n8n resume path via tunnel: "; curl -s -o /dev/null -w "%{http_code}\n" -H "ngrok-skip-browser-warning: 1" "https://$DOMAIN/webhook-waiting-slack" || true
echo "inspector: http://localhost:4040"
