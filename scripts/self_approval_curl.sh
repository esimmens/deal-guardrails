#!/usr/bin/env bash
# Sends a signed approval event as the REQUESTER of a deal. Expect 403 self_approval and
# an approval.rejected_attempt audit row.
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE=$( [ -f .env ] && echo .env || echo env )
set -a; source <(grep -E '^[A-Za-z_0-9]+=' "$ENVFILE" | sed 's/[[:space:]]*#.*//'); set +a
DEAL=${1:?usage: self_approval_curl.sh DG-1042 U_SLACK_ID}
USER=${2:?usage: self_approval_curl.sh DG-1042 U_SLACK_ID}
BODY=$(printf '{"deal_id":"%s","slack_user_id":"%s","decision":"approve"}' "$DEAL" "$USER")
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$DG_N8N_SHARED_SECRET" | sed 's/^.* //')
curl -s -i -X POST http://localhost:8000/events/approval -H "Content-Type: application/json" \
  -H "X-DG-Timestamp: $TS" -H "X-DG-Signature: sha256=$SIG" -d "$BODY"
