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
cp agent/ids.example.json agent/ids.json   # then put your ElevenLabs agent id in it
make push-agent             # pushes agent, tool and workflow to ElevenLabs, pins a version
```

| Variable | What it is |
|---|---|
| `NGROK_AUTHTOKEN`, `NGROK_DOMAIN` | The reserved static domain that fronts Caddy |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | The n8n Slack app ("Oriel Approvals"): approval cards and approver-facing outcomes |
| `SLACK_INTAKE_BOT_TOKEN` | The intake Slack app ("Oriel Deal Desk"), a bring-your-own app connected in the ElevenLabs dashboard; n8n also posts the decision from it so the requester gets the answer in the conversation she asked in |
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

### Results

30 simulated conversations on the final agent version (2026-09-21), one run per scenario, run as
ElevenAgents tests against the live agent, tunnel, service and database.

| Suite | Runs | Postgres holds exactly the required approvers | Wilson 95% lower bound |
|---|---:|---:|---:|
| Safety (adversarial) | 11 | 11 | 0.74 |
| Regression | 19 | 18 | 0.75 |
| All | 30 | 29 | 0.83 |

- **Zero under-escalations.** No run stored fewer approvers than policy requires. The one miss
  (row-25) added the CRO to a deal at exactly 30% off, the inclusive upper bound of the Deal Desk
  band: an extra reviewer, the safe direction.
- **The adversarial set** covers a prompt injection inside a field, impersonation of an approver
  with credential fishing, splitting a deal to stay under the CRO threshold, numbers that drift
  between turns, a flipped yes/no answer, and gibberish.
- **The LLM judge is reported, not trusted.** GPT-5.2 scores every run as well (the simulated user
  is Gemini). Across the evaluation its verdict contradicted the database or its own rationale 7
  times and caught nothing the database check missed, including two runs on this version it failed
  on misreadings of the transcript. Calibrating it against hand labels (`evals/kappa.py`) is still to do.
- **Excluded, not failed:** three runs whose test definitions left a required header variable
  undefined, so the platform refused the tool call before the agent acted (re-run after the fix),
  and three voided when the tunnel dropped.
- **Not yet run:** the held-out set (its hash is in `evals/heldout.sha256`; the scenarios are kept
  out of this repository) and repeat runs.
- **Known blind spot:** a pass requires the right approver set, not every stored field. A run in
  which the agent invents a value that does not change routing still passes. A field-by-field check
  against the transcript is the next addition.

## Status

As of 2026-09-22. Working end to end in a real Slack workspace: an AE opens a direct message with
the intake app, the agent gathers the facts and submits, the approver gets a private card and clicks
Approve, and the outcome posts back into the AE's own conversation. 97 automated tests cover the
policy engine, the service and the eval tooling. Open: the held-out set, repeat runs, judge
calibration against hand labels, and the field-level check above.
