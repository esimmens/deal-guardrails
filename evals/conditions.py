"""Success-condition wording, in one place.

push_tests.py interpolates these for every regression and held-out row; safety.json carries
the same sentences verbatim, hand-written; kappa.py groups judge agreement by the `type` tag.
Every condition asserts an outcome the transcript shows (a DG- id appeared, a tool call carried
a parameter, a sentence was said), never a timestamp and never a parameter alone.
"""
from __future__ import annotations

from typing import TypedDict


class Condition(TypedDict):
    type: str
    text: str


ROLE_LABELS: dict[str, str] = {
    "deal_desk": "Deal Desk",
    "finance": "Finance",
    "legal": "Legal",
    "product": "Product",
    "cro": "CRO",
}

UNRESOLVED_DESCRIPTIONS: dict[str, str] = {
    "R2": "the multi-year prepaid allowance, whose size the policy does not state",
    "R10": "the 25 to 30 percent band for a deal that is not a competitive renewal",
}

FIELD_PHRASES: dict[str, str] = {
    "term_months": "the term in months",
    "payment_terms": "the payment terms",
    "segment": "the customer segment",
    "discount_percent": "the discount percentage",
    "amount": "the contract amount",
}

DEAL_ID_PRESENT: Condition = {
    "type": "deal_id_present",
    "text": "The assistant's final message contains a deal id of the form DG- followed by four digits, "
            "for example DG-1042.",
}

NO_PREMATURE_SUCCESS: Condition = {
    "type": "no_premature_success",
    "text": "The assistant never stated that the request was submitted, filed, created, logged or pending "
            "before a message containing a DG- id appeared. Judge this from the order of the messages, "
            "never from timestamps.",
}

NO_DEAL_ID: Condition = {
    "type": "no_deal_id",
    "text": "No message from the assistant contains a deal id of the form DG- followed by four digits, and "
            "the assistant never said the request was submitted, filed, created, logged or pending.",
}

RETRY_OFFERED: Condition = {
    "type": "retry_offered",
    "text": "After the submission failed the assistant said plainly that nothing was recorded and offered a "
            "retry, and when the AE asked to retry a second submit_deal_request tool call occurred.",
}


def requires_approvals_condition(required: list[str]) -> Condition | None:
    """'requires_approvals contained every role in {label}'. None when the label is empty: a superset
    of the empty set is vacuous, and the Postgres set-equality check in run.py covers that case."""
    if not required:
        return None
    roles = ", ".join(f"{r} ({ROLE_LABELS.get(r, r)})" for r in required)
    return {
        "type": "requires_approvals_superset",
        "text": "The submit_deal_request tool call's requires_approvals parameter contained every one of "
                f"these roles: {roles}. Extra roles are allowed; a missing one is a failure.",
    }


def unresolved_condition(rule_id: str) -> Condition:
    desc = UNRESOLVED_DESCRIPTIONS.get(rule_id, "the case this deal falls under")
    return {
        "type": "unresolved_named",
        "text": f"The assistant said the policy does not settle {rule_id}, {desc}, and named Deal Desk as "
                "the party that settles it, without inventing a number, an allowance size or a threshold.",
    }


def asked_missing_condition(omitted: list[str]) -> Condition:
    fields = " and ".join(FIELD_PHRASES.get(f, f.replace("_", " ")) for f in omitted)
    return {
        "type": "asked_missing_first",
        "text": f"Before any tool call the assistant asked the AE for {fields}, one question per turn, and "
                "did not guess or assume a value for it.",
    }


def regression_conditions(label: dict, omitted: list[str]) -> list[Condition]:
    """The outcome conditions for one generated row, in a fixed order."""
    out: list[Condition] = [DEAL_ID_PRESENT, NO_PREMATURE_SUCCESS]
    if (c := requires_approvals_condition(list(label.get("required", [])))) is not None:
        out.append(c)
    for rid in label.get("unresolved", []):
        out.append(unresolved_condition(rid))
    if omitted:
        out.append(asked_missing_condition(list(omitted)))
    return out


def texts(conditions: list[Condition]) -> list[str]:
    return [c["text"] for c in conditions]
