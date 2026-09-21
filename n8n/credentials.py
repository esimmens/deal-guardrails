#!/usr/bin/env python
"""Create or update the n8n Slack credentials from the env file, without opening the n8n UI.

Two bots, two credentials, fixed ids so a re-run updates rather than duplicates:

  Oriel Approvals bot   SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET   (cards, approver outcomes, reminders)
  Oriel Deal Desk bot   SLACK_INTAKE_BOT_TOKEN                   (the decision back to the requester)

The intake bot only ever posts, so it needs no signing secret in n8n; clicks arrive on the
approvals bot. Tokens are read from the env file and handed to `n8n import:credentials`
through stdin inside the container; nothing is written to the repo or printed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CREDENTIALS = [
    {"id": "DGslackapprovals", "name": "Oriel Approvals bot",
     "token": "SLACK_BOT_TOKEN", "secret": "SLACK_SIGNING_SECRET"},
    {"id": "DGslackintake001", "name": "Oriel Deal Desk bot",
     "token": "SLACK_INTAKE_BOT_TOKEN", "secret": None},
]


def env_values() -> dict[str, str]:
    for name in (".env", "env"):
        f = ROOT / name
        if f.exists():
            out: dict[str, str] = {}
            for line in f.read_text().splitlines():
                line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("#") else ""
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
            return out
    return {}


def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    envfile = ".env" if (ROOT / ".env").exists() else "env"
    return subprocess.run(["docker", "compose", "--env-file", envfile, *args],
                          cwd=ROOT, input=stdin, capture_output=True, text=True)


def existing_ids() -> dict[str, str]:
    r = compose("exec", "-T", "n8n", "sh", "-lc",
                "n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1; "
                "cat /tmp/c.json; rm -f /tmp/c.json")
    try:
        return {c["name"]: c["id"] for c in json.loads(r.stdout) if c.get("type") == "slackApi"}
    except Exception:
        return {}


def main() -> int:
    env = env_values()
    have = existing_ids()
    payload = []
    for c in CREDENTIALS:
        token = env.get(c["token"])
        if not token:
            print(f"skip {c['name']!r}: {c['token']} is not set in the env file")
            continue
        data = {"accessToken": token}
        if c["secret"] and env.get(c["secret"]):
            data["signatureSecret"] = env[c["secret"]]
        # Keep whatever id the credential already has (the UI may have created it); otherwise use ours.
        payload.append({"id": have.get(c["name"], c["id"]), "name": c["name"], "type": "slackApi", "data": data})
    if not payload:
        return 1
    compose("exec", "-T", "n8n", "sh", "-lc", "umask 077; cat > /tmp/creds.json", stdin=json.dumps(payload))
    r = compose("exec", "-T", "n8n", "sh", "-lc",
                "n8n import:credentials --input=/tmp/creds.json; rc=$?; rm -f /tmp/creds.json; exit $rc")
    out = (r.stdout or r.stderr).strip()
    print(out.splitlines()[-1] if out else f"import rc={r.returncode}")
    for p in payload:
        print(f"  {p['name']!r} -> {p['id']}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
