"""Two trust boundaries.

Tool -> service: a static bearer the ElevenAgents webhook tool sends from a workspace secret.
n8n <-> service: HMAC-SHA256 over "<unix ts>.<raw body>" with a shared secret, a time window,
and a small replay cache. A Slack user id inside the body authenticates nobody; the
employees table does that, later, inside the transaction.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict

from fastapi import Header, HTTPException, Request

from .config import get_settings

_SEEN: OrderedDict[str, float] = OrderedDict()
_SEEN_MAX = 2048


def require_tool_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().dg_tool_token
    if not expected:
        raise HTTPException(500, {"error": "server_misconfigured", "detail": "DG_TOOL_TOKEN unset"})
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"error": "unauthorized"})
    if not hmac.compare_digest(authorization[7:].strip(), expected):
        raise HTTPException(401, {"error": "unauthorized"})


def sign(secret: str, timestamp: str | int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


async def verify_hmac(request: Request) -> bytes:
    """Dependency: returns the raw body on success, raises 401 on any failure."""
    settings = get_settings()
    if not settings.dg_n8n_shared_secret:
        raise HTTPException(500, {"error": "server_misconfigured", "detail": "DG_N8N_SHARED_SECRET unset"})
    ts = request.headers.get("x-dg-timestamp")
    sig = request.headers.get("x-dg-signature")
    if not ts or not sig:
        raise HTTPException(401, {"error": "unauthorized", "detail": "missing signature headers"})
    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(401, {"error": "unauthorized", "detail": "bad timestamp"}) from None
    now = int(time.time())
    if abs(now - ts_int) > settings.hmac_window_seconds:
        raise HTTPException(401, {"error": "unauthorized", "detail": "timestamp outside window"})
    body = await request.body()
    expected = sign(settings.dg_n8n_shared_secret, ts, body)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, {"error": "unauthorized", "detail": "signature mismatch"})
    if sig in _SEEN:
        raise HTTPException(401, {"error": "unauthorized", "detail": "replay"})
    _SEEN[sig] = time.time()
    while len(_SEEN) > _SEEN_MAX:
        _SEEN.popitem(last=False)
    return body
