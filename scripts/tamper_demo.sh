#!/usr/bin/env bash
# On camera: a superuser disables the append-only trigger, edits one audit row, re-enables
# the trigger, and the verifier still catches it. Tamper-evident, not tamper-proof.
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE=$( [ -f .env ] && echo .env || echo env )
PSQL="docker compose --env-file $ENVFILE exec -T postgres psql -v ON_ERROR_STOP=1 -U dg_admin -d dg"
SEQ=${1:-$($PSQL -tAc "SELECT seq FROM audit_events WHERE event_type='deal.submitted' ORDER BY seq DESC LIMIT 1")}
echo "editing audit row seq=$SEQ as dg_admin (trigger disabled for one statement)"
$PSQL <<SQL
ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable;
UPDATE audit_events SET payload = payload || '{"discount_bps": 500}'::jsonb WHERE seq = $SEQ;
ALTER TABLE audit_events ENABLE TRIGGER audit_events_immutable;
SQL
echo "now verifying the chain:"
set +e
uv run python service/verify_audit.py
RC=$?
set -e
echo "verifier exit code: $RC"
exit $RC
