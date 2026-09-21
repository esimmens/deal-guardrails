"""Plain-language explanations of why a rule fired, for the people who read receipts.

The evaluator speaks in rule ids (R1b, R3). An AE reading a Slack message, or an approver reading
a card, needs the sentence the id stands for, with the deal's own numbers in it. The id stays on
the end in brackets so the receipt page, the audit row and the policy document still line up.

This file is deliberately separate from `pricing_policy.yaml`: the policy version is the hash of
that file's bytes, and rewording an explanation is not a policy change.
"""
from __future__ import annotations

PAYMENT_LABELS = {
    "net_30": "Net-30",
    "net_45": "Net-45",
    "net_60": "Net-60",
    "net_90": "Net-90",
    "annual_prepaid": "Annual prepaid",
    "multi_year_prepaid": "Full-term prepaid",
    "other": "Non-standard",
}


def _pct(x: float | None) -> str:
    return "an unknown" if x is None else f"a {x:g}%"


def _usd(cents: int | None) -> str:
    return "an unknown amount" if cents is None else f"${cents / 100:,.0f}"


def explain_rule(rule_id: str, *, title: str = "", discount_percent: float | None = None,
                 payment_terms: str | None = None, term_months: int | None = None,
                 net_total_cents: int | None = None) -> str:
    """One sentence, in the deal's own numbers, ending with the rule id in brackets."""
    d = _pct(discount_percent)
    pay = PAYMENT_LABELS.get(payment_terms or "", payment_terms or "these")
    text = {
        "R1a": f"{d.capitalize()} discount is within the 15% an AE can approve alone",
        "R1b": f"{d.capitalize()} discount is above the 15% an AE can approve alone, so Deal Desk must sign off",
        "R1c": f"{d.capitalize()} discount is above 30%, so the CRO and Deal Desk must both sign off",
        "R2": (f"This is a {term_months if term_months is not None else 'multi'}-month deal paid up front with a "
               "discount above 15%. The policy says Deal Desk keeps a schedule of allowances for these but never "
               "states how large they are, so Deal Desk has to settle it"),
        "R3": f"{pay} payment terms are longer than the standard Net-30, so Finance must sign off",
        "R4": "Non-standard legal terms need Legal to sign off",
        "R5": ("Termination for convenience needs Legal and Deal Desk to sign off: it is a legal exposure and a "
               "revenue-recognition question"),
        "R6": "Outcome-based pricing needs Product and Finance to sign off",
        "R7": "An implementation arrangement needs Product to sign off",
        "R8": "Restructuring the license fee needs Finance to sign off",
        "R9": ("The AE says this is a strategic account. That is recorded, but only Deal Desk can designate one, "
               "so it changes nothing here"),
        "R10": (f"{d.capitalize()} discount sits in the 25 to 30% band on a deal that is not a competitive renewal. "
                "A March memo and the August policy disagree on whether that needs the CRO, so Deal Desk has to "
                "settle it"),
        "R11": f"The net contract value is {_usd(net_total_cents)}, over the $1,000,000 line, so the CRO must sign off",
        "R12": "Public-sector procurement terms need Legal to sign off",
    }.get(rule_id) or (title or "This rule applies")
    return f"{text} [{rule_id}]."
