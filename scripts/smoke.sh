#!/usr/bin/env bash
# Pre-take check: containers healthy, tunnel up, one synthetic round trip (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE=$( [ -f .env ] && echo .env || echo env )
# Load KEY=VALUE lines only, comments stripped. An explicit export loop: `source <(...)` with
# `set -a` loaded nothing when run from this Mac's shell, and the loop is easy to test.
while IFS= read -r line; do export "$line"; done < <(grep -E '^[A-Za-z_0-9]+=' "$ENVFILE" | sed 's/[[:space:]]*#.*//')
docker compose --env-file "$ENVFILE" ps --format 'table {{.Service}}\t{{.Status}}'
curl -sf http://localhost:8000/health >/dev/null && echo "service: ok" || { echo "service: DOWN"; exit 1; }
curl -sf -H "ngrok-skip-browser-warning: 1" "https://$NGROK_DOMAIN/health" >/dev/null && echo "tunnel: ok" || { echo "tunnel: DOWN"; exit 1; }
BODY='{"conversation_id":"smoke-fixed-conversation","account_name":"Smoke Test Co","amount_usd":1000,"discount_percent":5,"term_months":12,"payment_terms":"net_30","segment":"smb","raw_request_text":"smoke test, 5% on a small deal","requester_slack_user_id":"'"${SMOKE_REQUESTER_SLACK_ID:-}"'"}'
OUT=$(curl -s -X POST http://localhost:8000/deals -H "Authorization: Bearer $DG_TOOL_TOKEN" -H "Content-Type: application/json" -d "$BODY")
echo "round trip: $(echo "$OUT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("deal_id") or d)')"
