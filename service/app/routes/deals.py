from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_settings
from ..core import DealSubmission, SubmitError, submit_deal
from ..db import tx
from ..outbox import dispatch_once
from ..receipt import label
from ..security import require_tool_token

router = APIRouter()
_env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "templates")),
                   autoescape=select_autoescape(["html"]))

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def _parse_duration(text: str | None) -> timedelta | None:
    if not text:
        return None
    m = _DURATION.match(text.strip().upper())
    if not m:
        raise HTTPException(400, {"error": "validation", "detail": "older_than must look like PT4H or P1D"})
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


@router.post("/deals", dependencies=[Depends(require_tool_token)])
def post_deal(payload: DealSubmission, request: Request, background: BackgroundTasks):
    settings = get_settings()
    policy = request.app.state.policy
    actor = {"type": "tool", "id": "elevenagents", "conversation_id": payload.conversation_id}
    try:
        with tx() as cur:
            result = submit_deal(cur, payload, policy, settings, actor=actor)
    except SubmitError as e:
        raise HTTPException(e.status_code, {"error": e.code, "detail": e.detail}) from None
    if not result.duplicate and result.status == "pending_approval":
        background.add_task(dispatch_once, settings)
    return {
        "deal_id": result.deal_ref, "duplicate": result.duplicate, "status": result.status,
        "confirmation_line": result.confirmation_line, "receipt_summary": result.receipt_summary,
        "receipt": result.receipt,
    }


@router.get("/deals")
def list_deals(status: str = Query(default="pending_approval"), older_than: str | None = Query(default=None)):
    cutoff = None
    if (delta := _parse_duration(older_than)) is not None:
        cutoff = datetime.now(UTC) - delta
    with tx() as cur:
        cur.execute(
            """SELECT d.deal_ref, d.account_name_raw AS account, d.discount_bps, d.gate_role::text AS gate_role,
                      d.gate_employee_ids, d.submitted_at, d.status::text AS status,
                      EXTRACT(EPOCH FROM now() - d.submitted_at)::int AS age_seconds
               FROM deals d WHERE d.status = %s AND (%s::timestamptz IS NULL OR d.submitted_at <= %s)
               ORDER BY d.submitted_at""",
            (status, cutoff, cutoff),
        )
        rows = cur.fetchall()
        out = []
        for r in rows:
            cur.execute("SELECT full_name, slack_user_id FROM employees WHERE id = ANY(%s)", (list(r["gate_employee_ids"] or []),))
            holders = cur.fetchall()
            out.append({
                "deal_id": r["deal_ref"], "account": r["account"], "discount_percent": r["discount_bps"] / 100,
                "gate_role": r["gate_role"], "gate_label": label(r["gate_role"]) if r["gate_role"] else None,
                "gate_names": [h["full_name"] for h in holders],
                "gate_slack_user_ids": [h["slack_user_id"] for h in holders if h["slack_user_id"]],
                "submitted_at": r["submitted_at"].isoformat(), "age_seconds": r["age_seconds"], "status": r["status"],
            })
    return out


@router.get("/deals/{deal_ref}")
def get_deal(deal_ref: str, request: Request):
    with tx() as cur:
        cur.execute(
            """SELECT d.*, e.full_name AS requester_name, a.name AS account
               FROM deals d JOIN employees e ON e.id = d.requester_employee_id JOIN accounts a ON a.id = d.account_id
               WHERE d.deal_ref = %s""",
            (deal_ref.upper(),),
        )
        deal = cur.fetchone()
        if deal is None:
            raise HTTPException(404, {"error": "unknown_deal", "detail": deal_ref})
        cur.execute("""SELECT a.role::text AS role, a.decision::text AS decision, a.decided_at, e.full_name
                       FROM approvals a JOIN employees e ON e.id = a.approver_employee_id WHERE a.deal_id = %s""", (deal["id"],))
        approvals = cur.fetchall()
        cur.execute("SELECT seq, event_type, occurred_at, payload FROM audit_events WHERE deal_id = %s ORDER BY seq", (deal["id"],))
        events = cur.fetchall()
    body = {
        "deal_id": deal["deal_ref"], "status": deal["status"], "account": deal["account"],
        "requester": deal["requester_name"], "discount_percent": deal["discount_bps"] / 100,
        "list_total_usd": deal["list_total_cents"] / 100, "net_total_usd": deal["net_total_cents"] / 100,
        "term_months": deal["term_months"], "payment_terms": deal["payment_terms"], "segment": deal["segment"],
        "gate_role": deal["gate_role"], "submitted_at": deal["submitted_at"].isoformat(),
        "decided_at": deal["decided_at"].isoformat() if deal["decided_at"] else None,
        "policy_version": deal["policy_version"], "receipt": deal["receipt"],
        "approvals": [{"role": a["role"], "decision": a["decision"], "by": a["full_name"], "at": a["decided_at"].isoformat()}
                      for a in approvals],
        "audit": [{"seq": e["seq"], "type": e["event_type"], "at": e["occurred_at"].isoformat(), "payload": e["payload"]}
                  for e in events],
    }
    if "text/html" in request.headers.get("accept", "") and "application/json" not in request.headers.get("accept", ""):
        tpl = _env.get_template("deal.html")
        return HTMLResponse(tpl.render(d=body, label=label))
    return JSONResponse(body)
