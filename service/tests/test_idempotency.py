from service.app.core import submit_deal
from service.app.db import tx

from .helpers import ACTOR, submission


def test_same_facts_same_conversation_is_one_deal(policy, settings, admin):
    with tx() as cur:
        first = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with tx() as cur:
        second = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    assert first.duplicate is False and second.duplicate is True
    assert first.deal_ref == second.deal_ref == "DG-1001"
    assert second.confirmation_line.startswith("Already on file as DG-1001")
    assert admin.execute("SELECT count(*) n FROM deals").fetchone()["n"] == 1
    assert admin.execute("SELECT count(*) n FROM notifications").fetchone()["n"] == 1
    types = [r["event_type"] for r in admin.execute("SELECT event_type FROM audit_events ORDER BY seq")]
    assert types == ["deal.submitted", "deal.duplicate_returned"]


def test_new_conversation_or_new_facts_is_a_new_deal(policy, settings, admin):
    with tx() as cur:
        submit_deal(cur, submission(), policy, settings, actor=ACTOR)
        submit_deal(cur, submission(conversation_id="conv-test-2"), policy, settings, actor=ACTOR)
        submit_deal(cur, submission(discount_percent=23), policy, settings, actor=ACTOR)
    assert admin.execute("SELECT count(*) n FROM deals").fetchone()["n"] == 3


def test_account_name_normalisation_dedupes(policy, settings, admin):
    with tx() as cur:
        submit_deal(cur, submission(), policy, settings, actor=ACTOR)
        r = submit_deal(cur, submission(account_name="  calloway group inc."), policy, settings, actor=ACTOR)
    assert r.duplicate is True
    assert admin.execute("SELECT count(*) n FROM accounts").fetchone()["n"] == 1
