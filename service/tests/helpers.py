from __future__ import annotations

from service.app.core import DealSubmission


def submission(**kw) -> DealSubmission:
    base = dict(
        conversation_id="conv-test-1", requester_slack_user_id="U_MAREN", account_name="Calloway Group",
        amount_usd=600000, value_basis="list_total", discount_percent=22, term_months=36,
        payment_terms="annual_prepaid", segment="enterprise", requires_approvals="deal_desk",
        raw_request_text="need approval on Calloway, 600k at 22% off, 3 years prepaid",
    )
    base.update(kw)
    return DealSubmission(**base)


ACTOR = {"type": "tool", "id": "test"}
