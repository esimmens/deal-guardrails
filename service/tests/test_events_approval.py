import json
import time

import pytest

from service.app.core import ApprovalError, ApprovalEvent, record_decision, submit_deal
from service.app.db import tx
from service.app.security import sign

from .helpers import ACTOR, submission

SLACK_ACTOR = {"type": "slack_user", "id": "test"}


def _ev(ref, user, decision="approve"):
    return ApprovalEvent(deal_id=ref, slack_user_id=user, decision=decision)


def test_requester_cannot_approve_and_wrong_role_cannot_approve(policy, settings):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    assert r.status == "pending_approval" and r.receipt["gate_role"] == "deal_desk"
    with pytest.raises(ApprovalError) as e:
        with tx() as cur:
            record_decision(cur, _ev(r.deal_ref, "U_MAREN"), settings, actor=SLACK_ACTOR)
    assert e.value.code == "self_approval" and e.value.status_code == 403
    with pytest.raises(ApprovalError) as e:
        with tx() as cur:
            record_decision(cur, _ev(r.deal_ref, "U_DANA"), settings, actor=SLACK_ACTOR)
    assert e.value.code == "not_gate_role"
    with pytest.raises(ApprovalError) as e:
        with tx() as cur:
            record_decision(cur, _ev(r.deal_ref, "U_NOBODY"), settings, actor=SLACK_ACTOR)
    assert e.value.code == "unknown_slack_user" and e.value.status_code == 401


def test_gate_holder_approves_once_and_only_once(policy, settings, admin):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with tx() as cur:
        out = record_decision(cur, _ev(r.deal_ref, "U_PRIYA"), settings, actor=SLACK_ACTOR)
    assert out["recorded"] is True and out["status"] == "approved" and out["approver"] == "Priya Ramaswamy"
    with tx() as cur:
        again = record_decision(cur, _ev(r.deal_ref, "U_PRIYA", "reject"), settings, actor=SLACK_ACTOR)
    assert again["recorded"] is False and again["reason"] == "already_recorded" and again["by"] == "Priya Ramaswamy"
    assert admin.execute("SELECT status::text s FROM deals").fetchone()["s"] == "approved"
    kinds = [k["kind"] for k in admin.execute("SELECT kind::text kind FROM notifications ORDER BY id")]
    assert kinds == ["approval_card", "requester_dm"]
    types = [e["event_type"] for e in admin.execute("SELECT event_type FROM audit_events ORDER BY seq")]
    assert types == ["deal.submitted", "approval.recorded", "deal.transitioned"]


def test_reject_is_a_recorded_decision_too(policy, settings):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with tx() as cur:
        out = record_decision(cur, _ev(r.deal_ref, "U_PRIYA", "reject"), settings, actor=SLACK_ACTOR)
    assert out["status"] == "rejected"


def test_skip_level_when_the_only_holder_is_the_requester(policy, settings, admin):
    with tx() as cur:
        r = submit_deal(cur, submission(requester_slack_user_id="U_PRIYA", conversation_id="conv-priya"),
                        policy, settings, actor=ACTOR)
    assert "skip_level" in r.receipt["flags"]
    assert r.receipt["gate_names"] == ["Rafael Quintero"]
    with pytest.raises(ApprovalError) as e:
        with tx() as cur:
            record_decision(cur, _ev(r.deal_ref, "U_PRIYA"), settings, actor=SLACK_ACTOR)
    assert e.value.code == "self_approval"
    with tx() as cur:
        out = record_decision(cur, _ev(r.deal_ref, "U_RAFAEL"), settings, actor=SLACK_ACTOR)
    assert out["recorded"] is True and out["role"] == "deal_desk"


def test_db_trigger_is_independent_of_the_handler(policy, settings, admin):
    """Bypass the handler entirely: the database itself refuses a self-approval."""
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("INSERT INTO approvals (deal_id, role, approver_employee_id, decision, slack_user_id) "
                      "SELECT id, 'deal_desk', requester_employee_id, 'approve', 'U_MAREN' FROM deals WHERE deal_ref=%s",
                      (r.deal_ref,))


def _signed(settings, body: dict, ts: int | None = None):
    raw = json.dumps(body).encode()
    ts = ts or int(time.time())
    return raw, {"Content-Type": "application/json", "X-DG-Timestamp": str(ts),
                 "X-DG-Signature": sign(settings.dg_n8n_shared_secret, ts, raw)}


def test_http_event_requires_valid_signature_and_refuses_replay(client, policy, settings, admin):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    body = {"deal_id": r.deal_ref, "slack_user_id": "U_PRIYA", "decision": "approve"}
    assert client.post("/events/approval", json=body).status_code == 401
    raw, headers = _signed(settings, body, ts=int(time.time()) - 10_000)
    assert client.post("/events/approval", content=raw, headers=headers).status_code == 401
    raw, headers = _signed(settings, body)
    resp = client.post("/events/approval", content=raw, headers=headers)
    assert resp.status_code == 200 and resp.json()["recorded"] is True
    assert client.post("/events/approval", content=raw, headers=headers).status_code == 401  # replay


def test_http_refusal_is_audited(client, policy, settings, admin):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    raw, headers = _signed(settings, {"deal_id": r.deal_ref, "slack_user_id": "U_MAREN", "decision": "approve"})
    resp = client.post("/events/approval", content=raw, headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "self_approval"
    row = admin.execute("SELECT payload FROM audit_events WHERE event_type='approval.rejected_attempt'").fetchone()
    assert row and row["payload"]["reason"] == "self_approval"
