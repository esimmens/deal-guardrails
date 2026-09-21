# Deal Guardrails

An internal approval-routing agent for non-standard sales deals, built the way an internal
forward-deployed engineer would build it: a language model reads the messy request, code
decides who must approve, a named human approves, and every step is recorded in a chain
that can be checked later.

An Account Executive writes in Slack:

> need approval on Calloway Group, 600k list over 3 years at 22% off, they pay annually up front

The intake agent asks for whatever is missing, restates the deal, and submits it. The policy
engine names the approvers and the rules that required them. The approval card goes to the
right person. The receipt shows what the AE wrote, what the model read, and what code decided.

## What it is made of

| Layer | Role | Owns |
|---|---|---|
| ElevenAgents (Claude Sonnet 5, text only) | Slack intake: collects fields in a fixed order, one question per turn, never narrates success | The conversation |
| `service/` (FastAPI, Python 3.12) | The only writer. Idempotent submit, policy evaluation, approvals, notifications outbox, audit chain | All state |
| `policy/` | `pricing_policy.yaml` (machine rules, content-hash versioned) and `deal_approval_policy.md` (the prose the agent cites) | The rules |
| Postgres 15 | Deals, pinned approval requirements, decisions, an append-only hash-chained audit log | The truth |
| n8n (two workflows) | Posts the approval card, collects the click, sweeps reminders | Delivery only, never a decision |
| Caddy + ngrok | One public URL. n8n's Slack resume path goes to n8n, everything else to the service | The front door |

Design decisions worth knowing:

- **The agent cannot narrate its own success.** The workflow's tool node branches on the real
  HTTP result. The deal id and the confirmation sentence are authored by the server and handed
  to the confirmed node as dynamic variables. The workflow API has no node that emits text
  without a model, so the node is instructed to speak them verbatim and the eval checks the
  literal string; the id itself cannot be invented because it never passes through the model. The failure branch says nothing was
  recorded, and its retry is the same edge traversed backwards.
- **Duplicates return 200, never 409.** The idempotency key is the conversation id plus a
  digest of the business facts. A retry after a timeout whose row landed gets "already on file
  as DG-1042" rather than an error that would route the agent onto the failure branch and make
  it lie in the other direction.
- **The model may add a reviewer, never remove one.** Extracted booleans and human-confirmed
  values live in separate columns. Uncertainty escalates. Two rules in the policy are
  deliberately unresolved and route to Deal Desk by name.
- **A Slack user id authenticates. The employees table authorizes.** Self-approval is refused
  in the handler and again by a database trigger. Refused attempts are audited too.
- **Tamper-evident, not tamper-proof.** The audit table accepts inserts only, from a role that
  cannot update or delete, behind a trigger that blocks both. Every row hashes the previous
  one. A verifier walks the chain and exits nonzero on the first break, including after a
  superuser disables the trigger and edits a row.

## Run it locally

Requirements: Docker Desktop, `uv`, an ngrok account with one reserved domain, a Slack
workspace you administer, an ElevenLabs account.

```bash
cp .env.example .env        # fill in the values
make infra                  # postgres, n8n, caddy, ngrok
make up                     # builds and starts the service too
make seed                   # Oriel Speech: 8 people, 10 accounts, 41 historical deals
make test                   # policy properties, service, evals (offline parts)
make verify-audit           # walks the chain; exit 0
make tamper-demo            # edits one audit row as admin; verifier exits 1
make push-agent             # pushes agent, tool and workflow to ElevenLabs, pins a version
```

| Variable | What it is |
|---|---|
| `NGROK_AUTHTOKEN`, `NGROK_DOMAIN` | The reserved static domain that fronts Caddy |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | The n8n Slack app ("Oriel Approvals"); the ElevenAgents Slack app is connected in the ElevenLabs dashboard |
| `ELEVENLABS_API_KEY` | For `push-agent` and the eval runner |
| `OPENAI_API_KEY` | The non-Claude generator that writes eval scenario text |
| `DG_TOOL_TOKEN` | Bearer the ElevenAgents tool sends to `POST /deals` |
| `DG_N8N_SHARED_SECRET` | HMAC key for every call between n8n and the service |
| `PG_*_PASSWORD`, `N8N_ENCRYPTION_KEY` | Local infrastructure secrets |

## The fictional company

Oriel Speech sells text to speech, speech to text and conversational agent minutes. The price
ladder and the vocabulary of non-standard terms (outcome-based pricing, license fees,
implementation arrangements) mirror public material from a real vendor. Every discount
threshold is a labelled modelling assumption. `docs/derivation.md` says where each number came
from and where the sources contradict each other.

## Evaluation

`evals/` builds scenarios by reverse generation: a structured deal is drawn from a pairwise
covering array, the policy engine computes the expected approvals, and only then does a
non-Claude model write the AE's message. Tests assert outcomes (a deal id was issued, the
confirmed node was reached, the row in Postgres carries the expected approvals), never only
the tool call's parameters. A held-out set is hash-frozen. Results are reported per category
with Wilson lower bounds, alongside an ablation with the policy stripped from the prompt.
Numbers appear in `evals/results/published/` once a full run has been done, not before.

## Status

As of 2026-09-20, evening. Verified: the policy engine, schema, audit chain, service and seed
(94 tests); the agent, tool and workflow pushed to ElevenAgents; the full path Slack-shaped
test -> agent -> tunnel -> service -> Postgres, three times (DG-1042 to DG-1044). Not yet
verified: the agent on Claude. Every run so far was produced by the platform's default backup
models because the workspace ran out of credits, which the cascade hid until it was disabled
and the failure surfaced as a red. The n8n workflows import and parse but have not made a live
Slack round trip, and the evaluation suite has only run offline. The record of what was found,
including the mistakes, is `docs/build-log.md`.
