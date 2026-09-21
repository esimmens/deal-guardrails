"""Boundary table for the discount bands and every rule, one row per case."""
from pathlib import Path

import pytest

from policy.evaluate import evaluate, load_policy
from policy.schema import Facts

POLICY = load_policy(Path(__file__).resolve().parents[1] / "pricing_policy.yaml")


def f(**kw) -> Facts:
    base = dict(discount_bps=0, term_months=12, payment_terms="net_30", segment="enterprise",
                net_total_cents=50_000_00)
    base.update(kw)
    return Facts(**base)


CASES = [
    # id, facts, required (rank-desc order), unresolved, flags subset
    ("ae_1499", f(discount_bps=1499), [], [], {"within_ae_authority"}),
    ("ae_1500_boundary", f(discount_bps=1500), [], [], {"within_ae_authority"}),
    ("dd_1501", f(discount_bps=1501), ["deal_desk"], [], set()),
    ("dd_2500_competitive_renewal", f(discount_bps=2500, is_renewal=True, is_competitive=True), ["deal_desk"], [], set()),
    ("contested_2501_new_logo", f(discount_bps=2501, is_renewal=False, is_competitive=True), ["deal_desk"], ["R10"], set()),
    ("contested_3000_noncompetitive_renewal", f(discount_bps=3000, is_renewal=True, is_competitive=False), ["deal_desk"], ["R10"], set()),
    ("dd_3000_competitive_renewal_not_contested", f(discount_bps=3000, is_renewal=True, is_competitive=True), ["deal_desk"], [], set()),
    ("cro_3001", f(discount_bps=3001, is_renewal=True, is_competitive=True), ["cro", "deal_desk"], [], set()),
    ("multi_year_allowance_unresolved", f(discount_bps=2200, term_months=36, payment_terms="annual_prepaid"), ["deal_desk"], ["R2"], set()),
    ("multi_year_but_within_ae", f(discount_bps=1200, term_months=36, payment_terms="annual_prepaid"), [], [], {"within_ae_authority"}),
    ("multi_year_net30_not_prepaid", f(discount_bps=2200, term_months=36, payment_terms="net_30"), ["deal_desk"], [], set()),
    ("cheap_but_net60_needs_finance", f(discount_bps=800, payment_terms="net_60"), ["finance"], [], {"within_ae_authority"}),
    ("annual_prepaid_no_finance", f(discount_bps=800, payment_terms="annual_prepaid"), [], [], {"within_ae_authority"}),
    ("legal_terms", f(discount_bps=1200, nonstandard_legal_terms=True), ["legal"], [], set()),
    ("tfc_adds_legal_and_deal_desk", f(discount_bps=1200, termination_for_convenience=True), ["legal", "deal_desk"], [], set()),
    ("outcome_based", f(discount_bps=1000, outcome_based_pricing=True), ["finance", "product"], [], set()),
    ("implementation", f(discount_bps=1000, implementation_arrangement=True), ["product"], [], set()),
    ("license_restructure", f(discount_bps=1000, license_fee_restructure=True), ["finance"], [], set()),
    ("strategic_claim_never_reduces", f(discount_bps=2800, claims_strategic_account=True, is_renewal=False), ["deal_desk"], ["R10"], {"strategic_claim_unverified"}),
    ("large_deal_cro", f(discount_bps=1000, net_total_cents=100_000_000), ["cro"], [], set()),
    ("public_sector_legal", f(discount_bps=1000, segment="public_sector"), ["legal"], [], set()),
    ("stack_everything", f(discount_bps=3200, payment_terms="net_90", segment="public_sector",
                           termination_for_convenience=True, outcome_based_pricing=True, net_total_cents=200_000_000),
     ["cro", "legal", "finance", "product", "deal_desk"], [], set()),
]


@pytest.mark.parametrize("case_id,facts,required,unresolved,flags", CASES, ids=[c[0] for c in CASES])
def test_rule_table(case_id, facts, required, unresolved, flags):
    d = evaluate(facts, POLICY)
    assert d["required"] == required, d
    assert d["unresolved"] == unresolved, d
    assert flags <= set(d["flags"]), d
    assert d["gate_role"] == (required[0] if required else None)
    assert d["policy_version"].startswith("sha256:")


def test_every_yaml_rule_has_a_row_that_fires():
    fired = set()
    for _, facts, *_ in CASES:
        fired |= set(evaluate(facts, POLICY)["matched_rule_ids"])
    assert fired == set(POLICY.rule_ids), f"rules never exercised: {set(POLICY.rule_ids) - fired}"
