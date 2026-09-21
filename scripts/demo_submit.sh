#!/usr/bin/env bash
# Submits one deal straight to the service, as the agent's tool would, and lets the outbox
# deliver the approval card. Use it to exercise the card -> click -> decision path without
# spending an agent conversation; the intake wording is the agent's job, not this script's.
#
#   scripts/demo_submit.sh U0C3778CDRT        # Maren asks; Finance is the gate, so Dana decides
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE=$( [ -f .env ] && echo .env || echo env )
set -a; source <(grep -E '^[A-Za-z_0-9]+=' "$ENVFILE" | sed 's/[[:space:]]*#.*//'); set +a
REQUESTER=${1:?usage: demo_submit.sh U_REQUESTER_SLACK_ID}
# A fresh conversation id each run: two identical deals from one conversation are a duplicate,
# which is the behaviour we want everywhere except a repeated demo.
CONV="manual-$(date +%s)"
read -r -d '' BODY <<'JSON' || true
{
  "account_name": "Larkspur Media",
  "amount_usd": 200000,
  "value_basis": "list_total",
  "discount_percent": 19,
  "term_months": 12,
  "payment_terms": "net_60",
  "segment": "mid_market",
  "terms": {
    "termination_for_convenience": false,
    "nonstandard_legal_terms": false,
    "outcome_based_pricing": false,
    "implementation_arrangement": false,
    "license_fee_restructure": false,
    "claims_strategic_account": false,
    "is_renewal": false,
    "is_competitive": false
  },
  "requires_approvals": "deal_desk,finance",
  "raw_request_text": "need approval on Larkspur Media - 200k list over 12 months at 19% off, net 60",
  "llm_model": "none (scripts/demo_submit.sh)",
  "agent_version": "manual"
}
JSON
curl -s -X POST http://localhost:8000/deals \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DG_TOOL_TOKEN" \
  -H "X-DG-Conversation-Id: $CONV" \
  -H "X-DG-Requester-Slack-Id: $REQUESTER" \
  -d "$BODY" | python3 -m json.tool
