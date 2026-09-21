"""The decision receipt: what you wrote, what the model read, which rules fired.

Stored on the deal, posted in the Slack thread, rendered on the receipt page. The server
authors every sentence in it; the agent only repeats them."""
from __future__ import annotations

from datetime import datetime
from typing import Any

ROLE_LABELS = {
    "ae": "Account Executive",
    "deal_desk": "Deal Desk",
    "finance": "Finance",
    "legal": "Legal",
    "product": "Product",
    "cro": "CRO",
}

STATUS_PHRASE = {
    "pending_approval": "pending approval",
    "cleared": "recorded, no approval needed",
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "superseded": "superseded",
    "expired": "expired",
}


def label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def build_receipt(*, deal_ref: str, raw_request_text: str, model_read: dict[str, Any],
                  server_derived: dict[str, Any], rules_fired: list[dict], required: list[dict],
                  gate_role: str | None, gate_names: list[str], llm_added: list[str],
                  unresolved: list[dict], flags: list[str], policy_version: str,
                  receipt_url: str) -> dict[str, Any]:
    return {
        "deal_id": deal_ref,
        "you_wrote": raw_request_text,
        "model_read": model_read,
        "server_derived": server_derived,
        "rules_fired": rules_fired,
        "required": required,
        "gate_role": gate_role,
        "gate_label": label(gate_role) if gate_role else None,
        "gate_names": gate_names,
        "llm_added": llm_added,
        "unresolved": unresolved,
        "flags": flags,
        "policy_version": policy_version,
        "receipt_url": receipt_url,
    }


def receipt_summary_for(receipt: dict[str, Any] | None, *, approvals_channel: str = "#deal-approvals") -> str:
    """One paragraph the confirmed node speaks verbatim after the confirmation line."""
    parts: list[str] = []
    req = receipt["required"]
    if req:
        names = []
        for r in req:
            tag = " (gate"
            if r.get("is_gate"):
                if receipt.get("gate_names"):
                    tag += ", " + " or ".join(receipt["gate_names"])
                tag += ")"
                names.append(f"{label(r['role'])}{tag}")
            else:
                names.append(label(r["role"]))
        parts.append("Approvals required: " + ", ".join(names) + ".")
    else:
        parts.append("No approval is required; this is within your own authority and has been recorded.")
    fired = [r["id"] for r in receipt["rules_fired"]]
    parts.append("Rules fired: " + (", ".join(fired) if fired else "none") + ".")
    if receipt["unresolved"]:
        bits = [f"{u['id']} ({u['short']})" for u in receipt["unresolved"]]
        parts.append("Unresolved in policy: " + "; ".join(bits) + ". Deal Desk settles this.")
    if receipt["llm_added"]:
        parts.append("Added on the assistant's judgment: " + ", ".join(label(r) for r in receipt["llm_added"]) + ".")
    notable = [f for f in receipt["flags"] if f in ("strategic_claim_unverified", "skip_level", "no_gate_holder")
               or f.startswith("human_downgrade_blocked")]
    if notable:
        parts.append("Flags: " + ", ".join(notable) + ".")
    if req:
        parts.append(f"The approval card has been posted to {approvals_channel}. Nothing is approved yet.")
    return " ".join(parts)


def confirmation_line_for(deal_ref: str, status: str, *, duplicate: bool = False,
                          submitted_at: datetime | None = None, gate_role: str | None = None) -> str:
    if duplicate:
        when = submitted_at.strftime("%H:%M UTC") if submitted_at else "earlier"
        phrase = f"pending {label(gate_role)}" if status == "pending_approval" and gate_role else STATUS_PHRASE.get(status, status)
        return f"Already on file as {deal_ref} (submitted {when}, {phrase}). No second request was created."
    if status == "cleared":
        return f"Recorded as {deal_ref}. Nothing outside your own authority was requested, so no approval is needed."
    return f"Submitted as {deal_ref}."


SHORT_UNRESOLVED = {
    "R2": "the policy does not state the size of the multi-year prepaid allowance",
    "R10": "the 25 to 30 percent band conflicts with the March memo's competitive-renewal carve-out",
}
