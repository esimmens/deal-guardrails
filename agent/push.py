#!/usr/bin/env python
"""Push the agent-as-code to ElevenLabs in the order the platform tolerates.

1. Ensure a workspace secret holds the bearer the tool sends ("Bearer <DG_TOOL_TOKEN>").
2. Upsert the webhook tool (URL and secret substituted).
3. PATCH the agent's conversation_config + platform_settings (prompt assembled from
   prompt.md with the prose policy included).
4. PATCH the agent's workflow in a SEPARATE call. Sending both in one call returned HTTP 500
   during the spike.
5. Record the resulting version_id in ids.json.

--check diffs the live prompt and workflow against the files and exits 1 on drift.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).parent
API = "https://api.elevenlabs.io"


def env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for name in ("env", ".env"):
        p = ROOT / name
        if p.exists():
            vals.update({k: v for k, v in dotenv_values(p).items() if v})
    vals.update({k: v for k, v in os.environ.items() if k in ("ELEVENLABS_API_KEY", "NGROK_DOMAIN", "DG_TOOL_TOKEN")})
    return vals


def client(api_key: str) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"xi-api-key": api_key}, timeout=60.0)


def assemble_prompt() -> str:
    prompt = (HERE / "prompt.md").read_text()
    policy = (ROOT / "policy" / "deal_approval_policy.md").read_text()
    return prompt.replace("{{POLICY}}", "=== POLICY (cite rule ids in brackets) ===\n\n" + policy.strip())


def load_ids() -> dict:
    return json.loads((HERE / "ids.json").read_text())


def save_ids(ids: dict) -> None:
    (HERE / "ids.json").write_text(json.dumps(ids, indent=2) + "\n")


def ensure_secret(c: httpx.Client, ids: dict, token: str) -> str:
    if ids.get("tool_token_secret_id"):
        return ids["tool_token_secret_id"]
    r = c.post("/v1/convai/secrets", json={"type": "new", "name": "dg_tool_bearer", "value": f"Bearer {token}"})
    r.raise_for_status()
    sid = r.json()["secret_id"]
    ids["tool_token_secret_id"] = sid
    save_ids(ids)
    print(f"created workspace secret dg_tool_bearer -> {sid}")
    return sid


def tool_body(public_url: str, secret_id: str) -> dict:
    raw = (HERE / "tools" / "submit_deal_request.json").read_text()
    raw = raw.replace("${SERVICE_PUBLIC_URL}", public_url).replace("${DG_TOOL_TOKEN_SECRET_ID}", secret_id)
    return json.loads(raw)


def upsert_tool(c: httpx.Client, ids: dict, body: dict) -> str:
    if ids.get("tool_id"):
        r = c.patch(f"/v1/convai/tools/{ids['tool_id']}", json=body)
        if r.status_code == 404:
            ids["tool_id"] = None
        else:
            r.raise_for_status()
            return ids["tool_id"]
    r = c.post("/v1/convai/tools", json=body)
    r.raise_for_status()
    ids["tool_id"] = r.json()["id"]
    save_ids(ids)
    return ids["tool_id"]


def agent_config(prompt: str) -> dict:
    cfg = json.loads((HERE / "agent.json").read_text())
    cfg["conversation_config"]["agent"]["prompt"]["prompt"] = prompt
    return cfg


def workflow_body(tool_id: str) -> dict:
    raw = (HERE / "workflow.json").read_text().replace("${TOOL_ID}", tool_id)
    return json.loads(raw)


def push(check_only: bool = False) -> int:
    e = env()
    api_key = e.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ELEVENLABS_API_KEY is not set in env/.env", file=sys.stderr)
        return 2
    public_url = f"https://{e['NGROK_DOMAIN']}"
    ids = load_ids()
    prompt = assemble_prompt()
    with client(api_key) as c:
        if check_only:
            live = c.get(f"/v1/convai/agents/{ids['agent_id']}").json()
            drift = []
            if live["conversation_config"]["agent"]["prompt"]["prompt"] != prompt:
                drift.append("prompt")
            wf = workflow_body(ids.get("tool_id") or "")
            live_wf = live.get("workflow") or {}
            if set(live_wf.get("nodes", {})) != set(wf["nodes"]) or set(live_wf.get("edges", {})) != set(wf["edges"]):
                drift.append("workflow")
            print("drift:", drift or "none", "| live version:", live.get("version_id"))
            return 1 if drift else 0
        secret_id = ensure_secret(c, ids, e["DG_TOOL_TOKEN"])
        tool_id = upsert_tool(c, ids, tool_body(public_url, secret_id))
        print(f"tool {tool_id} -> {public_url}/deals")
        cfg = agent_config(prompt)
        cfg["conversation_config"]["agent"]["prompt"]["tool_ids"] = []  # the tool lives on the workflow node only
        if ids.get("agent_id"):
            r = c.patch(f"/v1/convai/agents/{ids['agent_id']}", json={"name": cfg["name"], "tags": cfg["tags"],
                        "conversation_config": cfg["conversation_config"], "platform_settings": cfg["platform_settings"]})
            r.raise_for_status()
        else:
            r = c.post("/v1/convai/agents/create", json=cfg)
            r.raise_for_status()
            ids["agent_id"] = r.json()["agent_id"]
            save_ids(ids)
        print(f"agent {ids['agent_id']} config pushed")
        r = c.patch(f"/v1/convai/agents/{ids['agent_id']}", json={"workflow": workflow_body(tool_id)})
        r.raise_for_status()
        version = r.json().get("version_id")
        ids["version_id"] = version
        save_ids(ids)
        print(f"workflow pushed; pinned version {version}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    sys.exit(push(check_only=ap.parse_args().check))
