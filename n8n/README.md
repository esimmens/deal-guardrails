# n8n workflows

Two exported workflows for the n8n container in `docker-compose.yml` (image `n8nio/n8n:latest`, which resolved to
n8n **2.39.8** on 2026-09-20; node package `n8n-nodes-base` 2.39.6). n8n owns Slack here and nothing else: the
service decides, n8n displays and relays.

| File | Purpose |
| --- | --- |
| `deal-card.workflow.json` | Two entry points. `POST /webhook/deal-card` turns an `approval_card` notification into a Slack card with Approve and Decline buttons, waits for the click, signs it, and reports it to `POST /events/approval`. `POST /webhook/notify` relays `requester_dm` and `thread_update` notifications. |
| `reminder-sweep.workflow.json` | Every 5 minutes: `POST /notifications/dispatch` (drains the outbox), then `GET /deals?status=pending_approval&older_than=PT4H` and posts a "Still waiting" line per deal to `#deal-approvals`. |

Node type versions used (all are the defaults n8n 2.39.8 creates in the editor): Webhook 2.1, Code 2,
Slack 2.7, If 2.3, Switch 3.4, HTTP Request 4.5, Schedule Trigger 1.4, Split Out 1.

## 1. Compose changes (required)

Add two lines to the `n8n` service `environment:` block in `docker-compose.yml`, then recreate the container
(`docker compose --env-file env up -d n8n`):

```yaml
      NODE_FUNCTION_ALLOW_BUILTIN: "crypto"
      N8N_BLOCK_ENV_ACCESS_IN_NODE: "false"
```

Why both:

- `NODE_FUNCTION_ALLOW_BUILTIN: "crypto"` lets the Code nodes `require('crypto')` for HMAC-SHA256. n8n passes this
  variable through to its JavaScript task runner (runners are on in 2.x; `N8N_RUNNERS_ENABLED` is now deprecated
  and ignored, the default mode is `internal`).
- `N8N_BLOCK_ENV_ACCESS_IN_NODE: "false"` is needed because in this n8n build `$env` is blocked unless the variable
  is explicitly `false`. This was verified inside the running container: `createEnvProviderState()` returns
  `isEnvAccessBlocked: true` with the current environment and `false` only with this setting. The docs table still
  lists the default as `false`; the code in 2.39.8 (`n8n-workflow/dist/cjs/workflow-data-proxy-env-provider.js`)
  says otherwise. Without it every Code node fails with "access to env vars denied" and nothing is verified or
  signed.

`DG_N8N_SHARED_SECRET` is already set on the container; the Code nodes read it as `$env.DG_N8N_SHARED_SECRET`.

If you would rather not expose `process.env` to the Code node at all, the alternative is the Crypto node v2, which
reads its HMAC secret from a Crypto credential instead of `$env`. That needs a credential created by hand in the
editor and was not used here because the task specified the Code node.

## 2. Import

The repo's `n8n/` directory is mounted read-only at `/n8n` inside the container:

```sh
docker compose --env-file env exec -T n8n n8n import:workflow --input=/n8n/deal-card.workflow.json
docker compose --env-file env exec -T n8n n8n import:workflow --input=/n8n/reminder-sweep.workflow.json
```

Both files carry fixed workflow ids (`DGdealcard000001`, `DGremindersweep1`), so re-importing after an edit
updates the existing workflow instead of creating a duplicate. The CLI import always leaves workflows inactive.

Then create the Slack credential once, and link it with the importer:

1. Open http://localhost:5678 and finish the owner setup if this is a fresh instance.
2. Create a credential of type **Slack API** named exactly `Oriel Approvals bot`:
   **Access Token** = `SLACK_BOT_TOKEN` (the `xoxb-` bot token), **Signature Secret** =
   `SLACK_SIGNING_SECRET`. The Signature Secret is what verifies button clicks arriving at
   `/webhook-waiting-slack`; without it the Slack node answers 401 to every click.
   In n8n 2.x there is no Credentials entry in the sidebar: open any Slack node in the `deal-card`
   workflow and choose **Create new credential** in its credential dropdown.
3. Run the importer, which links every Slack node to that credential by id and publishes both
   workflows:

```sh
uv run python n8n/import.py
docker compose --env-file env restart n8n   # n8n asks for this after a CLI import
```

The workflow JSON in this repo deliberately references the credential by **name** only, so the files
stay portable between machines. n8n resolves credentials by id, so a name-only reference publishes
with "Credential not configured" on every Slack node. `import.py` looks the id up from the running
instance and injects it at import time, which keeps the repo clean and the import reproducible.

Note for n8n 2.x: the old Save button and Active toggle are gone. **Publish** does both, and the CLI
equivalent is `n8n publish:workflow --id=<id>`, not the deprecated `update:workflow --active=true`.

## 3. Slack app requirements

Bot token scopes: `chat:write`, `chat:write.public`, `users:read`, `users:read.email`, `channels:history`,
`channels:read`. `chat:write` covers both channel posts and DMs (Slack opens the DM when a user id is passed as the
channel); `chat:write.public` lets the bot post in `#deal-approvals` without being invited; `users:read` and
`users:read.email` let n8n resolve who clicked.

Interactivity: **Features > Interactivity & Shortcuts**, enabled, Request URL
`https://<NGROK_DOMAIN>/webhook-waiting-slack`. Paste the app's Signing Secret (Settings > Basic Information) into
the credential's Signature Secret field.

## 4. What is public

Only the Send-and-Wait resume paths need to be reachable from Slack: `/webhook-waiting-slack` (button clicks,
verified with the Slack signing secret) and `/webhook-waiting/*` (the URL-button fallback n8n uses when responder
capture is off). `Caddyfile` already proxies exactly those paths to `n8n:5678`; everything else on the ngrok
domain goes to the service. `/webhook/deal-card` and `/webhook/notify` are never public: the service calls
`http://n8n:5678/webhook/...` on the compose network, and a request to those paths on the public domain lands on
the service, which has no such routes.

## 5. How the deal-card workflow runs

1. **Deal card webhook** (`POST /webhook/deal-card`, responds immediately so the service outbox marks the row
   sent, Raw Body on).
2. **Verify signature** (Code): rejects timestamps more than 300 s from now, recomputes
   `sha256=HMAC(secret, "<ts>.<raw body>")` over the raw bytes (the service signs canonical JSON that a
   re-serialisation would not reproduce), compares in constant time, throws on mismatch, outputs the parsed body.
3. **Build Block Kit** (Code): builds the Block Kit `blocks` (header `DG-1042 · Account`, deal fields, "Approvals
   required" with `(this card)` on the gate role and `(displayed, not wired in this build)` on the others, "Rules
   fired", bold "Unresolved in policy: R2 (short)" lines, a context block with the receipt link and flags, and the
   gate approver mentions) plus a mrkdwn `text` rendering of the same content. `<`, `>` and `&` in service-supplied
   strings are escaped.
4. **Slack approval card** (Send and Wait for Response, Approval, Approve/Decline, Capture Who Responded on,
   Restrict Who Can Approve = the deal's `gate_slack_user_ids`, After Decision = show outcome and remove buttons,
   Limit Wait Time 7 days). On a click the node outputs
   `data.{approved, responder.{id,name,username,email}, respondedAt, channel, messageId}`.
5. **Responded?** (If): a 7-day timeout resumes the node with its input passed through (no `data`), so the false
   branch posts a "No decision within 7 days" note and sends nothing to the service.
6. **Sign approval event** (Code): builds the exact JSON string
   `{"deal_id","slack_user_id","decision","slack_message_ts","responded_at"}` (`approve`/`reject`, responder id,
   the card's `ts`, Slack's `action_ts` as ISO time), signs `"<ts>.<body>"`, outputs `{body, ts, sig}`.
7. **POST approval to service** (HTTP Request, raw body = that string, headers `Content-Type`, `X-DG-Timestamp`,
   `X-DG-Signature`, Never Error + full response, Continue on error): 4xx is data, not a crash.
8. **Recorded?** (If) on `body.recorded === true`: thread reply "Recorded: DG-1042 approved by Priya Ramaswamy at
   16:12 UTC. Requester notified." on the card, or "Not recorded: DG-1042. not_gate_role: ..." with the service's
   `reason`/`error`/`detail`.

Second entry point: **Notify webhook** (`POST /webhook/notify`) > the same **Verify signature** code > **Route by
kind** (Switch): `requester_dm` posts `payload.text` to `target.slack_user_id`; `thread_update` posts it to
`target.channel_id` as a reply to `target.thread_ts`. Any other kind is dropped (no fallback output).

## 6. n8n holds no decision state

Every click is resolved by the service, which locks and re-reads the deal row inside one transaction, maps the Slack
user id to an employee, checks the gate role and the self-approval rule, inserts the decision with `ON CONFLICT DO
NOTHING`, and only then transitions the deal and queues the requester DM and thread update. n8n keeps a paused
execution while it waits for the button and forwards what Slack reported; if that execution is lost, restarted, or
times out, no approval exists and nothing is undone. A second click by anyone is answered by the service with
`recorded: false, reason: already_recorded` and posted as such. The Restrict Who Can Approve list on the Slack node
is a courtesy filter (an ephemeral "not authorized" reply); the service enforces the same rule regardless.

## 7. Known limitations

- **Block Kit in Send and Wait.** The Send and Wait for Response operation renders only its Message parameter as a
  single mrkdwn section (plus the buttons); it does not accept custom blocks. The card is therefore sent as the
  mrkdwn `text` built in Build Block Kit. The full `blocks` array is still produced and left on the item, so a plain
  Send node (Message Type: Blocks) can post it if a richer card is wanted; that node would then need the
  Send-and-Wait message sent as a thread reply on it.
- **Responder capture.** The responder's Slack id is captured (`data.responder.id`) only in Approval mode with
  Capture Who Responded on, a Signature Secret in the credential, and Interactivity pointed at
  `/webhook-waiting-slack`. If any of these is missing, n8n falls back to plain link buttons and reports no
  responder. In that case Sign approval event throws and nothing is sent: the fallback of attributing the click to
  the first gate approver was deliberately not implemented, because an unattributed click must never become a
  recorded decision.
- **Restrict Who Can Approve** is evaluated at click time from the Build Block Kit output. An empty
  `gate_slack_user_ids` list means anyone can click; the service still rejects non-gate users with 403
  `not_gate_role`. None of these Slack node options carry a license check in 2.39.8's node code, so they work in the
  Community edition.
- **Timeout.** After 7 days the waiting execution resumes with the node passed through; the workflow posts a note
  and stops. The deal stays `pending_approval` in the service and appears in the sweep until decided.
- **Replay cache.** The service rejects a signature it has already seen. The sweep signs a fresh timestamp each run,
  so two runs would only collide if they started within the same second.
- **Raw body.** The Webhook node exposes the raw request body as binary property `data`; Verify signature reads it
  with `helpers.getBinaryDataBuffer` (falls back to the inline base64). Do not switch the Webhook node's Raw Body
  option off.
- **Expressions after a wait.** Nodes after the Slack card reference earlier nodes explicitly
  (`$('Build Block Kit')`, `$('Slack approval card')`) because `$json` after a resume is the Slack output, not the
  card data.

## 8. What was verified, and what was not

Verified:

- Both files parse as JSON and `n8n import:workflow` inside the running container accepted them (imported inactive,
  no credentials created, nothing activated).
- The exact n8n version (2.39.8) and every node typeVersion and parameter key used, read from the node description
  files shipped in the container (`n8n-nodes-base` 2.39.6), including the Slack Send-and-Wait fields
  (`captureResponder`, `approvers`, `unauthorizedReplyText`, `postDecisionBehavior`, `options.limitWaitTime`), the
  Slack HITL webhook output shape, the route `/webhook-waiting-slack`, and that `helpers.getBinaryDataBuffer` and
  `require` of allowed built-ins are available to the Code node under the task runner.
- The `$env` blocking default described in section 1, by running `createEnvProviderState()` in the container.

Not verified (needs the Slack workspace, the ngrok domain and the two compose lines in place):

- A live round trip: card posted, button clicked, `/events/approval` accepted, thread reply and DM delivered.
- The rendered look of the mrkdwn card in Slack.
- Behaviour of the 7-day timeout branch (reasoned from `workflow-execute.js`, which disables the waiting node on a
  timed resume so its input passes through).
