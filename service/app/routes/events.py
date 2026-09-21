from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import ValidationError

from ..config import get_settings
from ..core import ApprovalError, ApprovalEvent, audit_refusal, record_decision
from ..db import tx
from ..outbox import dispatch_once
from ..security import verify_hmac

router = APIRouter()


@router.post("/events/approval")
def approval_event(background: BackgroundTasks, body: bytes = Depends(verify_hmac)):
    try:
        event = ApprovalEvent.model_validate(json.loads(body or b"{}"))
    except (ValidationError, json.JSONDecodeError) as e:
        raise HTTPException(400, {"error": "validation", "detail": str(e)[:500]}) from None
    settings = get_settings()
    actor = {"type": "slack_user", "id": event.slack_user_id, "via": "n8n"}
    try:
        with tx() as cur:
            result = record_decision(cur, event, settings, actor=actor)
    except ApprovalError as e:
        with tx() as cur:  # the refusal is audited in its own committed transaction
            audit_refusal(cur, event, e, actor)
        raise HTTPException(e.status_code, {"error": e.code, "detail": e.detail}) from None
    if result.get("recorded"):
        background.add_task(dispatch_once, settings)
    return result
