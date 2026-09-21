#!/usr/bin/env python
"""Import both workflows into the running n8n and publish them.

The workflow JSON in this repo references Slack credentials by NAME only, so the files stay
portable. n8n resolves credentials by id, not name, so this looks each id up from the running
instance and injects it at import time. Two credentials are expected, one per bot:

  "Oriel Approvals bot"  the n8n app: approval cards, outcomes to the approver, reminders
  "Oriel Deal Desk bot"  the intake app: the decision back to the requester, in the same
                         conversation she used to submit

Create them first (`n8n/credentials.py` from the env file, or the UI) with exactly those names.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ["n8n/deal-card.workflow.json", "n8n/reminder-sweep.workflow.json"]


def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    envfile = ".env" if (ROOT / ".env").exists() else "env"
    cmd = ["docker", "compose", "--env-file", envfile, *args]
    return subprocess.run(cmd, cwd=ROOT, input=stdin, capture_output=True, text=True)


def credential_ids() -> dict[str, str]:
    r = compose("exec", "-T", "n8n", "sh", "-lc",
                "n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1; "
                "cat /tmp/c.json; rm -f /tmp/c.json")
    try:
        return {c["name"]: c["id"] for c in json.loads(r.stdout) if c.get("type") == "slackApi"}
    except Exception:
        print(r.stdout[:300], r.stderr[:300], file=sys.stderr)
        return {}


def main() -> int:
    ids = credential_ids()
    wanted = set()
    for rel in WORKFLOWS:
        for node in json.loads((ROOT / rel).read_text()).get("nodes", []):
            name = ((node.get("credentials") or {}).get("slackApi") or {}).get("name")
            if name:
                wanted.add(name)
    missing = sorted(wanted - set(ids))
    if missing:
        print(f"n8n has no Slack credential named {missing}; create it first "
              f"(uv run python n8n/credentials.py)", file=sys.stderr)
        return 1
    for name in sorted(wanted):
        print(f"credential {name!r} -> {ids[name]}")
    for rel in WORKFLOWS:
        wf = json.loads((ROOT / rel).read_text())
        linked = 0
        for node in wf.get("nodes", []):
            creds = node.get("credentials") or {}
            if "slackApi" in creds:
                name = creds["slackApi"]["name"]
                creds["slackApi"] = {"id": ids[name], "name": name}
                linked += 1
        print(f"  {rel}: linked {linked} Slack node(s)")
        compose("exec", "-T", "n8n", "sh", "-lc", "cat > /tmp/wf.json", stdin=json.dumps(wf))
        r = compose("exec", "-T", "n8n", "sh", "-lc",
                    "n8n import:workflow --input=/tmp/wf.json; rc=$?; rm -f /tmp/wf.json; exit $rc")
        print("   ", (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else "imported")
        p = compose("exec", "-T", "n8n", "n8n", "publish:workflow", f"--id={wf['id']}")
        out = (p.stdout or p.stderr).strip()
        print("   ", out.splitlines()[-1] if out else f"publish rc={p.returncode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
