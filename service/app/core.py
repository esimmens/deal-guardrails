"""Core operations. The HTTP routes and the seed script both call these; nothing here
knows about HTTP. Each function expects to be called inside one transaction."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg import Cursor
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from policy.evaluate import Policy, evaluate, gate_for, merge_required, merge_terms, parse_requires_approvals
from policy.schema import TERM_FIELDS, Facts, PaymentTerms, Segment

from . import audit
from .approvers import employee_by_slack, gate_holders, roles_of
from .config import Settings
from .idempotency import canonical_digest, idempotency_key, normalize_account_name
from .outbox import enqueue
from .receipt import SHORT_UNRESOLVED, build_receipt, confirmation_line_for, label, receipt_summary_for
from .transitions import InvalidTransition, apply_transition


class Terms(BaseModel):
    termination_for_convenience: bool = False
    nonstandard_legal_terms: bool = False
    outcome_based_pricing: bool = False
    implementation_arrangement: bool = False
    license_fee_restructure: bool = False
    claims_strategic_account: bool = False
    is_renewal: bool = False
    is_competitive: bool = False


class DealSubmission(BaseModel):
    conversation_id: str | None = None
    requester_slack_user_id: str | None = None
    account_name: str = Field(min_length=1, max_length=200)
    amount_usd: float = Field(gt=0)
    value_basis: Literal["list_total", "net_total", "acv", "per_unit"] = "list_total"
    discount_percent: float = Field(ge=0, le=100)
    term_months: int = Field(gt=0, le=120)
    payment_terms: PaymentTerms
    segment: Segment
    terms: Terms = Field(default_factory=Terms)
    requires_approvals: str | None = None
    raw_request_text: str = Field(min_length=1, max_length=4000)
    llm_model: str | None = None
    agent_version: str | None = None


class ApprovalEvent(BaseModel):
    deal_id: str = Field(min_length=3)  # the deal_ref, e.g. DG-1042
    slack_user_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    slack_message_ts: str | None = None
    responded_at: datetime | None = None


class SubmitError(Exception):
    def __init__(self, status_code: int, code: str, detail: Any = None) -> None:
        super().__init__(code)
        self.status_code, self.code, self.detail = status_code, code, detail


class ApprovalError(SubmitError):
    pass


@dataclasses.dataclass
class SubmitResult:
    deal_id: int
    deal_ref: str
    duplicate: bool
    status: str
    confirmation_line: str
    receipt_summary: str
    receipt: dict[str, Any]


def derive_amounts(amount_usd: float, basis: str, discount_percent: float, term_months: int) -> tuple[int, int, int]:
    """Returns (list_total_cents, net_total_cents, acv_cents). The AE states one number and
    what it is; the server does the arithmetic, once, and records it."""
    d = discount_percent / 100.0
    if basis in ("list_total", "per_unit"):
        list_usd = amount_usd
        net_usd = amount_usd * (1 - d)
    elif basis == "net_total":
        net_usd = amount_usd
        list_usd = amount_usd / (1 - d) if d < 1 else amount_usd
    else:  # acv: annual net value
        net_usd = amount_usd * term_months / 12.0
        list_usd = net_usd / (1 - d) if d < 1 else net_usd
    list_cents = round(list_usd * 100)
    net_cents = round(net_usd * 100)
    acv_cents = round(net_cents * 12 / term_months)
    return max(list_cents, 1), max(net_cents, 0), acv_cents


def _resolve_requester(cur: Cursor, slack_user_id: str | None, settings: Settings) -> tuple[dict[str, Any], str]:
    if slack_user_id:
        emp = employee_by_slack(cur, slack_user_id)
        if not emp:
            raise SubmitError(400, "unknown_requester", f"no active employee with slack id {slack_user_id}")
        return emp, "slack"
    if settings.dg_default_requester_slack_id:
        emp = employee_by_slack(cur, settings.dg_default_requester_slack_id)
        if emp:
            return emp, "default"
    raise SubmitError(400, "unknown_requester", "no requester id supplied and no default configured")


def _upsert_account(cur: Cursor, name: str, segment: str) -> dict[str, Any]:
    norm = normalize_account_name(name)
    cur.execute(
        """INSERT INTO accounts (name, name_normalized, segment) VALUES (%s, %s, %s)
           ON CONFLICT (name_normalized) DO NOTHING RETURNING id, name, name_normalized, is_strategic""",
        (name.strip(), norm, segment),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id, name, name_normalized, is_strategic FROM accounts WHERE name_normalized = %s", (norm,))
        row = cur.fetchone()
    return row


def submit_deal(cur: Cursor, payload: DealSubmission, policy: Policy, settings: Settings, *,
                actor: dict[str, Any], occurred_at: datetime | None = None,
                slack_channel_id: str | None = None, slack_thread_ts: str | None = None) -> SubmitResult:
    now = occurred_at or datetime.now(UTC)
    requester, requester_source = _resolve_requester(cur, payload.requester_slack_user_id, settings)
    account = _upsert_account(cur, payload.account_name, payload.segment.value)

    list_cents, net_cents, acv_cents = derive_amounts(payload.amount_usd, payload.value_basis,
                                                      payload.discount_percent, payload.term_months)
    discount_bps = round(payload.discount_percent * 100)
    llm_terms = payload.terms.model_dump()
    merged_terms, merge_flags = merge_terms(llm_terms, {})
    facts = Facts(discount_bps=discount_bps, term_months=payload.term_months,
                  payment_terms=payload.payment_terms, segment=payload.segment,
                  net_total_cents=net_cents, **merged_terms)
    decision = evaluate(facts, policy)
    claimed = parse_requires_approvals(payload.requires_approvals)
    required, llm_added = merge_required(decision["required"], claimed, policy)
    gate_role = gate_for(required, policy)
    flags = list(decision["flags"]) + merge_flags + [f"llm_added:{r}" for r in llm_added]
    if requester_source == "default":
        flags.append("requester_source:default")
    if merged_terms["claims_strategic_account"] and account["is_strategic"]:
        flags.append("strategic_designation_on_file")

    digest = canonical_digest(facts, account["name_normalized"], list_cents)
    key = idempotency_key(payload.conversation_id, digest)
    status = "pending_approval" if required else "cleared"

    holders: list[dict[str, Any]] = []
    if gate_role:
        holders, gate_flags = gate_holders(cur, gate_role, requester)
        flags += gate_flags

    cur.execute(
        """INSERT INTO deals (requester_employee_id, account_id, account_name_raw, list_total_cents, net_total_cents,
                              acv_cents, discount_bps, term_months, payment_terms, segment, status, policy_version,
                              idempotency_key, conversation_id, raw_request_text, gate_role, gate_employee_ids, submitted_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING id, deal_ref""",
        (requester["id"], account["id"], payload.account_name.strip(), list_cents, net_cents, acv_cents,
         discount_bps, payload.term_months, payload.payment_terms.value, payload.segment.value, status,
         policy.version, key, payload.conversation_id, payload.raw_request_text, gate_role,
         [h["id"] for h in holders], now),
    )
    inserted = cur.fetchone()
    if inserted is None:
        cur.execute("SELECT id, deal_ref, status::text AS status, submitted_at, gate_role::text AS gate_role, receipt "
                    "FROM deals WHERE idempotency_key = %s", (key,))
        existing = cur.fetchone()
        audit.append(cur, deal_id=existing["id"], event_type="deal.duplicate_returned", actor=actor,
                     payload={"deal_ref": existing["deal_ref"], "idempotency_key": key,
                              "conversation_id": payload.conversation_id}, occurred_at=now)
        line = confirmation_line_for(existing["deal_ref"], existing["status"], duplicate=True,
                                     submitted_at=existing["submitted_at"], gate_role=existing["gate_role"])
        receipt = existing["receipt"] or {}
        return SubmitResult(existing["id"], existing["deal_ref"], True, existing["status"], line,
                            receipt_summary_for(receipt) if receipt else "", receipt)

    deal_id, deal_ref = int(inserted["id"]), inserted["deal_ref"]

    cur.execute(
        """INSERT INTO deal_terms (deal_id, termination_for_convenience, nonstandard_legal_terms, outcome_based_pricing,
             implementation_arrangement, license_fee_restructure, claims_strategic_account, is_renewal, is_competitive)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (deal_id, *[merged_terms[k] for k in TERM_FIELDS]),
    )

    model_read = {
        "account_name": payload.account_name, "amount_usd": payload.amount_usd, "value_basis": payload.value_basis,
        "discount_percent": payload.discount_percent, "term_months": payload.term_months,
        "payment_terms": payload.payment_terms.value, "segment": payload.segment.value,
        "requires_approvals": payload.requires_approvals, **llm_terms,
    }
    for field, value in model_read.items():
        cur.execute(
            "INSERT INTO request_extractions (deal_id, field, llm_value, llm_model, agent_version, source) VALUES (%s,%s,%s,%s,%s,'llm')",
            (deal_id, field, Jsonb(value), payload.llm_model, payload.agent_version),
        )
    server_derived = {"list_total_cents": list_cents, "net_total_cents": net_cents, "acv_cents": acv_cents,
                      "discount_bps": discount_bps, "requester_employee_id": requester["id"],
                      "requester_source": requester_source}
    for field, value in server_derived.items():
        cur.execute(
            "INSERT INTO request_extractions (deal_id, field, confirmed_value, source) VALUES (%s,%s,%s,'server_derived')",
            (deal_id, field, Jsonb(value)),
        )

    policy_roles = {r for rule in decision["rules_fired"] for r in _required_by(rule)}
    fired_index = {rule["id"]: rule for rule in decision["rules_fired"]}
    required_rows: list[dict[str, Any]] = []
    for role in required:
        rule_ids = [rule["id"] for rule in decision["rules_fired"] if role in _required_by(rule)]
        if role in policy_roles:
            added_by = "policy"
        elif role in llm_added:
            added_by = "llm"
        else:
            added_by = "unresolved"
            rule_ids = list(decision["unresolved"])
        is_gate = role == gate_role
        cur.execute(
            "INSERT INTO approval_requirements (deal_id, role, is_gate, rule_ids, added_by) VALUES (%s,%s,%s,%s,%s)",
            (deal_id, role, is_gate, rule_ids, added_by),
        )
        required_rows.append({"role": role, "label": label(role), "is_gate": is_gate, "added_by": added_by,
                              "rule_ids": rule_ids})

    if payload.conversation_id:
        cur.execute(
            """INSERT INTO conversations (conversation_id, deal_id, slack_channel_id, slack_thread_ts, requester_slack_user_id, agent_version)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (conversation_id) DO UPDATE SET deal_id = EXCLUDED.deal_id""",
            (payload.conversation_id, deal_id, slack_channel_id, slack_thread_ts,
             payload.requester_slack_user_id, payload.agent_version),
        )

    unresolved = [{"id": rid, "short": SHORT_UNRESOLVED.get(rid, "the policy does not settle this case"),
                   "note": fired_index[rid].get("note")} for rid in decision["unresolved"]]
    receipt = build_receipt(
        deal_ref=deal_ref, raw_request_text=payload.raw_request_text, model_read=model_read,
        server_derived=server_derived,
        rules_fired=[{"id": r["id"], "title": r["title"], "effect": r["effect"]} for r in decision["rules_fired"]],
        required=required_rows, gate_role=gate_role, gate_names=[h["full_name"] for h in holders],
        llm_added=llm_added, unresolved=unresolved, flags=flags, policy_version=policy.version,
        receipt_url=f"{settings.public_base_url.rstrip('/')}/deals/{deal_ref}",
    )
    cur.execute("UPDATE deals SET receipt = %s WHERE id = %s", (Jsonb(receipt), deal_id))

    audit.append(cur, deal_id=deal_id, event_type="deal.submitted", actor=actor, occurred_at=now, payload={
        "deal_ref": deal_ref, "status": status, "required": required, "gate_role": gate_role,
        "gate_employee_ids": [h["id"] for h in holders], "matched_rule_ids": decision["matched_rule_ids"],
        "unresolved": decision["unresolved"], "llm_added": llm_added, "flags": flags,
        "policy_version": policy.version, "idempotency_key": key, "discount_bps": discount_bps,
        "net_total_cents": net_cents, "requester_employee_id": requester["id"],
    })

    if status == "pending_approval":
        enqueue(cur, deal_id=deal_id, kind="approval_card",
                target={"channel": settings.slack_channel_approvals},
                payload={
                    "deal_id": deal_ref, "account": payload.account_name.strip(),
                    "requester": {"name": requester["full_name"], "title": requester["title"],
                                  "slack_user_id": requester.get("slack_user_id")},
                    "list_total_usd": list_cents / 100, "net_total_usd": net_cents / 100,
                    "discount_percent": payload.discount_percent, "term_months": payload.term_months,
                    "payment_terms": payload.payment_terms.value, "segment": payload.segment.value,
                    "terms": merged_terms, "required": required_rows, "gate_role": gate_role,
                    "gate_label": label(gate_role) if gate_role else None,
                    "gate_slack_user_ids": [h["slack_user_id"] for h in holders if h.get("slack_user_id")],
                    "gate_names": [h["full_name"] for h in holders],
                    "rules_fired": receipt["rules_fired"], "unresolved": unresolved, "flags": flags,
                    "receipt_url": receipt["receipt_url"], "you_wrote": payload.raw_request_text,
                    "thread": {"channel_id": slack_channel_id, "thread_ts": slack_thread_ts},
                })

    line = confirmation_line_for(deal_ref, status)
    return SubmitResult(deal_id, deal_ref, False, status, line, receipt_summary_for(receipt), receipt)


def _required_by(rule: dict[str, Any]) -> list[str]:
    return [seg.split(" ", 1)[1] for seg in rule["effect"].split("; ") if seg.startswith("require ")]


def record_decision(cur: Cursor, event: ApprovalEvent, settings: Settings, *, actor: dict[str, Any],
                    occurred_at: datetime | None = None) -> dict[str, Any]:
    """One transaction: lock the row, authorize, insert the decision, transition, audit, notify.
    Raises ApprovalError for every refusal; the caller audits the refused attempt separately."""
    now = occurred_at or datetime.now(UTC)
    cur.execute("SELECT * FROM deals WHERE deal_ref = %s FOR UPDATE", (event.deal_id,))
    deal = cur.fetchone()
    if deal is None:
        raise ApprovalError(404, "unknown_deal", event.deal_id)
    approver = employee_by_slack(cur, event.slack_user_id)
    if approver is None:
        raise ApprovalError(401, "unknown_slack_user", event.slack_user_id)
    if approver["id"] == deal["requester_employee_id"]:
        raise ApprovalError(403, "self_approval", "the requester cannot approve their own deal")
    gate_role = deal["gate_role"]
    if not gate_role:
        raise ApprovalError(409, "nothing_to_approve", f"deal is {deal['status']}")
    designated = list(deal["gate_employee_ids"] or [])
    if approver["id"] not in designated and gate_role not in roles_of(cur, approver["id"]):
        raise ApprovalError(403, "not_gate_role", f"{approver['full_name']} does not hold {gate_role} for this deal")
    try:
        new_status = apply_transition(deal["status"], event.decision)
    except InvalidTransition:
        raise ApprovalError(409, "invalid_transition", {"status": deal["status"]}) from None

    try:
        cur.execute(
            """INSERT INTO approvals (deal_id, role, approver_employee_id, decision, slack_user_id, slack_message_ts, decided_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (deal_id, role) DO NOTHING RETURNING id""",
            (deal["id"], gate_role, approver["id"], event.decision, event.slack_user_id, event.slack_message_ts, now),
        )
    except CheckViolation as exc:  # the trigger disagreed with us; that is the point of having it
        raise ApprovalError(403, "db_guard_refused", str(exc).splitlines()[0]) from None
    row = cur.fetchone()
    if row is None:
        cur.execute("""SELECT a.decided_at, e.full_name FROM approvals a JOIN employees e ON e.id = a.approver_employee_id
                       WHERE a.deal_id = %s AND a.role = %s""", (deal["id"], gate_role))
        prior = cur.fetchone()
        return {"recorded": False, "reason": "already_recorded", "deal_id": deal["deal_ref"],
                "by": prior["full_name"], "at": prior["decided_at"].isoformat(), "status": deal["status"]}

    cur.execute("UPDATE deals SET status = %s, decided_at = %s WHERE id = %s", (new_status, now, deal["id"]))
    audit.append(cur, deal_id=deal["id"], event_type="approval.recorded", actor=actor, occurred_at=now, payload={
        "deal_ref": deal["deal_ref"], "role": gate_role, "decision": event.decision,
        "approver_employee_id": approver["id"], "approver": approver["full_name"],
        "slack_user_id": event.slack_user_id, "slack_message_ts": event.slack_message_ts,
    })
    audit.append(cur, deal_id=deal["id"], event_type="deal.transitioned", actor=actor, occurred_at=now, payload={
        "deal_ref": deal["deal_ref"], "from": deal["status"], "event": event.decision, "to": new_status,
    })
    cur.execute("SELECT full_name, slack_user_id FROM employees WHERE id = %s", (deal["requester_employee_id"],))
    requester = cur.fetchone()
    msg = (f"{deal['deal_ref']} ({deal['account_name_raw']}) was {new_status} by {approver['full_name']} "
           f"({label(gate_role)}) at {now.strftime('%H:%M UTC')}.")
    if requester.get("slack_user_id"):
        enqueue(cur, deal_id=deal["id"], kind="requester_dm", target={"slack_user_id": requester["slack_user_id"]},
                payload={"text": msg, "deal_id": deal["deal_ref"], "status": new_status})
    cur.execute("SELECT slack_channel_id, slack_thread_ts FROM conversations WHERE deal_id = %s LIMIT 1", (deal["id"],))
    conv = cur.fetchone()
    if conv and conv.get("slack_thread_ts"):
        enqueue(cur, deal_id=deal["id"], kind="thread_update",
                target={"channel_id": conv["slack_channel_id"], "thread_ts": conv["slack_thread_ts"]},
                payload={"text": msg, "deal_id": deal["deal_ref"], "status": new_status})
    return {"recorded": True, "deal_id": deal["deal_ref"], "status": new_status, "approver": approver["full_name"],
            "role": gate_role, "decision": event.decision}


def audit_refusal(cur: Cursor, event: ApprovalEvent, err: ApprovalError, actor: dict[str, Any]) -> None:
    cur.execute("SELECT id FROM deals WHERE deal_ref = %s", (event.deal_id,))
    row = cur.fetchone()
    audit.append(cur, deal_id=row["id"] if row else None, event_type="approval.rejected_attempt", actor=actor,
                 payload={"deal_ref": event.deal_id, "slack_user_id": event.slack_user_id, "decision": event.decision,
                          "reason": err.code, "detail": err.detail if isinstance(err.detail, (str, dict, list)) else str(err.detail)})


def transition_deal(cur: Cursor, deal_id: int, event: str, *, actor: dict[str, Any],
                    occurred_at: datetime | None = None, supersedes_with: int | None = None) -> str:
    """Non-approval transitions (cancel, supersede, expire). Locks, validates, audits."""
    now = occurred_at or datetime.now(UTC)
    cur.execute("SELECT id, deal_ref, status::text AS status FROM deals WHERE id = %s FOR UPDATE", (deal_id,))
    deal = cur.fetchone()
    if deal is None:
        raise SubmitError(404, "unknown_deal", deal_id)
    try:
        new_status = apply_transition(deal["status"], event)
    except InvalidTransition:
        raise SubmitError(409, "invalid_transition", {"status": deal["status"], "event": event}) from None
    cur.execute("UPDATE deals SET status = %s, decided_at = %s WHERE id = %s", (new_status, now, deal_id))
    if supersedes_with is not None:
        cur.execute("UPDATE deals SET supersedes_deal_id = %s WHERE id = %s", (deal_id, supersedes_with))
    audit.append(cur, deal_id=deal_id, event_type="deal.transitioned", actor=actor, occurred_at=now, payload={
        "deal_ref": deal["deal_ref"], "from": deal["status"], "event": event, "to": new_status,
        "superseded_by_deal_id": supersedes_with,
    })
    return new_status
