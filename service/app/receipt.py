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


def receipt_summary_for(receipt: dict[str, Any] | None) -> str:
    """One paragraph the confirmed node speaks verbatim after the confirmation line.

    Approval requests are sent privately to the person who must decide. Nothing about a deal is
    posted to a shared channel: the commercial terms are the point of the request, and a channel
    would show them to everyone in it and invite the wrong person to click.
    """
    req = receipt["required"]
    whys = [r.get("why") or f"{r.get('title', r['id'])} [{r['id']}]." for r in receipt["rules_fired"]]
    if not req:
        why = " ".join(whys) if whys else "Nothing here is outside an AE's own authority."
        return f"No approval is needed. {why} It has been recorded."

    lines: list[str] = ["Why this needs approval: " + " ".join(whys)]
    if receipt["llm_added"]:
        added = ", ".join(label(r) for r in receipt["llm_added"])
        lines.append(f"The assistant also asked for {added} to review, on its own judgment; policy did not require it.")

    roles = [label(r["role"]) for r in req]
    gate = label(receipt["gate_role"]) if receipt.get("gate_role") else None
    names = " or ".join(receipt.get("gate_names") or [])
    if "no_gate_holder" in receipt["flags"] or not names:
        who = (f"Who signs off: {_join(roles)}. Nobody currently holds the {gate or 'deciding'} role, so this is "
               "waiting to be routed. Nothing is approved yet.")
    elif len(roles) == 1:
        who = f"Who signs off: {names} ({gate}). The request has gone to {names} privately. Nothing is approved yet."
    else:
        who = (f"Who signs off: {_join(roles)}. {gate} ranks highest, so the request has gone to {names} ({gate}) "
               "to decide. Nothing is approved yet.")
    lines.append(who)

    other = [f for f in receipt["flags"] if f == "skip_level" or f.startswith("human_downgrade_blocked")]
    if other:
        lines.append("Flags for Deal Desk: " + ", ".join(other) + ".")
    return "\n".join(lines)


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


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
