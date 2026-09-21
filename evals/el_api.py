"""Every HTTP call to the ElevenLabs conversational-AI test API, in one small module.

Paths were checked against the installed SDK (elevenlabs 2.68.0, conversational_ai/tests/*
and agents/raw_client.py). If a path moves, fix it here.

Importable without credentials: the key is read only when a client is constructed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Self

import httpx

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.elevenlabs.io"

PATH_TEST_CREATE = "/v1/convai/agent-testing/create"
PATH_TEST_LIST = "/v1/convai/agent-testing"
PATH_TEST = "/v1/convai/agent-testing/{test_id}"  # GET, PUT (update), DELETE
PATH_FOLDER_CREATE = "/v1/convai/agent-testing/folders"
PATH_FOLDER = "/v1/convai/agent-testing/folders/{folder_id}"
PATH_RUN_TESTS = "/v1/convai/agents/{agent_id}/run-tests"
PATH_INVOCATION = "/v1/convai/test-invocations/{invocation_id}"
PATH_AGENT = "/v1/convai/agents/{agent_id}"

ENV_KEYS = ("ELEVENLABS_API_KEY", "OPENAI_API_KEY", "SLACK_ID_MAREN", "NGROK_DOMAIN", "DG_TOOL_TOKEN")


class ElevenLabsAPIError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:800]}")
        self.status, self.method, self.path, self.body = status, method, path, body


def load_env() -> dict[str, str]:
    """Values from the repo's `env` / `.env` files, then the process environment on top.
    Only the keys the eval pipeline needs; nothing is exported back into os.environ."""
    vals: dict[str, str] = {}
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover
        dotenv_values = None  # type: ignore[assignment]
    if dotenv_values is not None:
        for name in ("env", ".env"):
            p = ROOT / name
            if p.exists():
                vals.update({k: v for k, v in dotenv_values(p).items() if v})
    vals.update({k: v for k, v in os.environ.items() if k in ENV_KEYS and v})
    return vals


def api_key_from_env() -> str | None:
    return load_env().get("ELEVENLABS_API_KEY")


class ElevenLabsTests:
    """Thin client. Every method returns plain dicts (the JSON the API sent)."""

    def __init__(self, api_key: str | None = None, *, base_url: str = API_BASE, timeout: float = 60.0,
                 client: httpx.Client | None = None) -> None:
        key = api_key or api_key_from_env()
        if client is None and not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set (env, .env or the environment)")
        self._client = client or httpx.Client(base_url=base_url, headers={"xi-api-key": key or ""},
                                              timeout=timeout)

    # ---- plumbing -------------------------------------------------------------------------

    def _request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                 retries: int = 2) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries + 1):
            r = self._client.request(method, path, json=json, params=clean_params)
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise ElevenLabsAPIError(r.status_code, method, path, r.text)
            if not r.content:
                return None
            return r.json()
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- tests -------------------------------------------------------------------------------

    def list_tests(self, *, search: str | None = None, parent_folder_id: str | None = None,
                   include_folders: bool | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        """All pages of GET /v1/convai/agent-testing. Folders come back as entries with
        entity_type == "folder" when include_folders is true."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self._request("GET", PATH_TEST_LIST, params={
                "search": search, "parent_folder_id": parent_folder_id,
                "include_folders": include_folders, "page_size": page_size, "cursor": cursor,
            })
            out.extend(page.get("tests", []))
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return out

    def create_test(self, body: dict[str, Any]) -> str:
        return str(self._request("POST", PATH_TEST_CREATE, json=body)["id"])

    def update_test(self, test_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", PATH_TEST.format(test_id=test_id), json=body) or {}

    def get_test(self, test_id: str) -> dict[str, Any]:
        return self._request("GET", PATH_TEST.format(test_id=test_id))

    def delete_test(self, test_id: str) -> None:
        self._request("DELETE", PATH_TEST.format(test_id=test_id))

    # ---- folders -----------------------------------------------------------------------------

    def create_folder(self, name: str, parent_folder_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if parent_folder_id:
            body["parent_folder_id"] = parent_folder_id
        return self._request("POST", PATH_FOLDER_CREATE, json=body)

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        return self._request("GET", PATH_FOLDER.format(folder_id=folder_id))

    def find_folder(self, name: str, parent_folder_id: str | None = None) -> dict[str, Any] | None:
        """Top-level folder by exact name, or None."""
        for entry in self.list_tests(search=name, parent_folder_id=parent_folder_id, include_folders=True):
            is_folder = entry.get("entity_type") == "folder" or entry.get("type") == "folder"
            if is_folder and entry.get("name") == name and not (entry.get("folder_parent_id") or parent_folder_id):
                return entry
            if is_folder and entry.get("name") == name and entry.get("folder_parent_id") == parent_folder_id:
                return entry
        return None

    # ---- runs --------------------------------------------------------------------------------

    def run_tests(self, agent_id: str, test_ids: list[str], *, repeat_count: int = 1,
                  agent_config_override: dict[str, Any] | None = None,
                  branch_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"tests": [{"test_id": t} for t in test_ids], "repeat_count": repeat_count}
        if agent_config_override is not None:
            body["agent_config_override"] = agent_config_override
        if branch_id:
            body["branch_id"] = branch_id
        return self._request("POST", PATH_RUN_TESTS.format(agent_id=agent_id), json=body)

    def get_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self._request("GET", PATH_INVOCATION.format(invocation_id=invocation_id))

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", PATH_AGENT.format(agent_id=agent_id))


def invocation_complete(inv: dict[str, Any]) -> bool:
    """bucketing_status is None when repeat_count == 1 (no bucketing); then every run must have
    left 'pending'. Otherwise wait for 'completed' (or 'failed', which also ends the wait)."""
    status = inv.get("bucketing_status")
    runs = inv.get("test_runs") or []
    if status in ("completed", "failed"):
        return True
    if status is None:
        return bool(runs) and all(r.get("status") != "pending" for r in runs)
    return False


PLATFORM_ERROR_PATTERNS: tuple[str, ...] = ("insufficient credits", "failed to generate a response", "all llm attempts were exhausted")


def platform_error(texts) -> str | None:
    """The platform's own failure string when a run died for reasons that have nothing to do with the
    agent (credits exhausted, model unavailable). Such runs are voided, never counted as agent failures."""
    if isinstance(texts, dict):
        texts = [*(texts.get("messages") or []), str(texts.get("summary") or "")]
    if isinstance(texts, str):
        texts = [texts]
    for t in texts or []:
        low = str(t).lower()
        for p in PLATFORM_ERROR_PATTERNS:
            if p in low:
                return str(t)[:160]
    return None


INFRA_ERROR_SIGNATURES: tuple[str, ...] = ("ERR_NGROK_", "assets.ngrok.com", "Request timed out", "ngrok-free.dev")


def infra_error(submit_errors) -> str | None:
    """A failure of OUR test infrastructure (the tunnel, a gateway timeout) on a real submit call, as
    opposed to a mocked failure the scenario asked for. Such runs say nothing about the agent and are
    reported as infra errors, outside every rate. `submit_errors` is a list of (error_type, raw_message)."""
    import re
    for etype, raw in submit_errors or []:
        raw = str(raw or "")
        for sig in INFRA_ERROR_SIGNATURES:
            if sig in raw:
                code = re.search(r"ERR_NGROK_\d+", raw)
                return (code.group(0) + " (tunnel endpoint offline)" if code and code.group(0).endswith("3200")
                        else code.group(0) + " (tunnel gateway error)" if code
                        else f"{etype or 'error'}: {raw[:80]}")
    return None
