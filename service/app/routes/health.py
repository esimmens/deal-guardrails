from fastapi import APIRouter, Request

from .. import audit
from ..db import tx

router = APIRouter()


@router.get("/health")
def health(request: Request):
    policy = request.app.state.policy
    with tx() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
        head = audit.head(cur)
    return {"ok": True, "policy_version": policy.version, "policy_label": policy.version_label,
            "db": "ok", "audit_head": head}
