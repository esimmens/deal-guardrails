from service.app.core import Terms, submit_deal
from service.app.db import tx

from .helpers import ACTOR, submission


def test_calloway_receipt_names_the_unresolved_rule(policy, settings):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    rec = r.receipt
    assert [x["id"] for x in rec["rules_fired"]] == ["R1b", "R2"]
    assert rec["unresolved"][0]["id"] == "R2"
    assert rec["required"] == [{"role": "deal_desk", "label": "Deal Desk", "is_gate": True, "added_by": "policy",
                                "rule_ids": ["R1b"]}]
    # The spoken receipt explains each rule in the deal's own numbers and keeps the id in brackets.
    assert "A 22% discount is above the 15% an AE can approve alone, so Deal Desk must sign off [R1b]." in r.receipt_summary
    assert "so Deal Desk has to settle it [R2]." in r.receipt_summary
    assert "Who signs off: Priya Ramaswamy (Deal Desk)." in r.receipt_summary
    assert "gate" not in r.receipt_summary and "Rules fired" not in r.receipt_summary
    assert r.confirmation_line == "Submitted as DG-1001."


def test_the_aha_case_discount_inside_guardrail_terms_outside(policy, settings):
    s = submission(conversation_id="conv-aha", account_name="Tamsin Logistics", amount_usd=95000,
                   discount_percent=12, term_months=12, payment_terms="net_30", segment="mid_market",
                   terms=Terms(termination_for_convenience=True), requires_approvals=None,
                   raw_request_text="12% on Tamsin, standard 12 months, they want termination for convenience")
    with tx() as cur:
        r = submit_deal(cur, s, policy, settings, actor=ACTOR)
    assert r.status == "pending_approval"
    assert "within_ae_authority" in r.receipt["flags"]
    assert [x["role"] for x in r.receipt["required"]] == ["legal", "deal_desk"]
    assert r.receipt["gate_role"] == "legal" and r.receipt["gate_names"] == ["Silas Wren"]


def test_llm_may_add_a_reviewer_but_the_engine_records_who_added_it(policy, settings):
    s = submission(conversation_id="conv-llm-add", requires_approvals="deal_desk, finance")
    with tx() as cur:
        r = submit_deal(cur, s, policy, settings, actor=ACTOR)
    by = {x["role"]: x["added_by"] for x in r.receipt["required"]}
    assert by == {"finance": "llm", "deal_desk": "policy"}
    assert r.receipt["gate_role"] == "finance" and "llm_added:finance" in r.receipt["flags"]


def test_within_authority_is_recorded_not_routed(policy, settings, admin):
    s = submission(conversation_id="conv-clear", amount_usd=20000, discount_percent=5, term_months=12,
                   payment_terms="net_30", requires_approvals=None, raw_request_text="5% on a small one")
    with tx() as cur:
        r = submit_deal(cur, s, policy, settings, actor=ACTOR)
    assert r.status == "cleared" and r.confirmation_line.startswith("Recorded as DG-1001")
    assert admin.execute("SELECT count(*) n FROM notifications").fetchone()["n"] == 0
    assert admin.execute("SELECT count(*) n FROM approval_requirements").fetchone()["n"] == 0


def test_extractions_keep_model_and_server_values_apart(policy, settings, admin):
    with tx() as cur:
        submit_deal(cur, submission(value_basis="net_total", amount_usd=468000), policy, settings, actor=ACTOR)
    rows = {(r["field"], r["source"]): r for r in admin.execute(
        "SELECT field, source::text source, llm_value, confirmed_value FROM request_extractions")}
    assert rows[("amount_usd", "llm")]["llm_value"] == 468000 and rows[("amount_usd", "llm")]["confirmed_value"] is None
    assert rows[("list_total_cents", "server_derived")]["confirmed_value"] == 60000000
    assert rows[("discount_bps", "server_derived")]["confirmed_value"] == 2200
