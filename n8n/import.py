#!/usr/bin/env python
"""Import both workflows into the running n8n and publish them.

The workflow JSON in this repo references the Slack credential by NAME only, so the files stay
portable. n8n resolves credentials by id, not name, so this looks the id up from the running
instance and injects it at import time. Create the credential first (UI, or `n8n
import:credentials`) named exactly CRED_NAME.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRED_NAME = "Oriel Approvals bot"
WORKFLOWS = ["n8n/deal-card.workflow.json", "n8n/reminder-sweep.workflow.json"]


def compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    envfile = ".env" if (ROOT / ".env").exists() else "env"
    cmd = ["docker", "compose", "--env-file", envfile, *args]
    return subprocess.run(cmd, cwd=ROOT, input=stdin, capture_output=True, text=True)


def credential_id(name: str) -> str | None:
    r = compose("exec", "-T", "n8n", "sh", "-lc",
                "n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1; "
                "cat /tmp/c.json; rm -f /tmp/c.json")
    try:
        for c in json.loads(r.stdout):
            if c.get("name") == name:
                return c.get("id")
    except Exception:
        print(r.stdout[:300], r.stderr[:300], file=sys.stderr)
    return None


def main() -> int:
    cid = credential_id(CRED_NAME)
    if not cid:
        print(f"no credential named {CRED_NAME!r} in n8n; create it first", file=sys.stderr)
        return 1
    print(f"credential {CRED_NAME!r} -> {cid}")
    for rel in WORKFLOWS:
        wf = json.loads((ROOT / rel).read_text())
        linked = 0
        for node in wf.get("nodes", []):
            creds = node.get("credentials") or {}
            if "slackApi" in creds:
                creds["slackApi"] = {"id": cid, "name": CRED_NAME}
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
