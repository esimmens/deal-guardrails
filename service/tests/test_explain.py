"""Every rule the policy can fire has a plain-language sentence, and the sentence carries the deal's numbers."""
from pathlib import Path

import yaml

from service.app.explain import explain_rule

POLICY = Path(__file__).resolve().parents[2] / "policy" / "pricing_policy.yaml"


def test_every_policy_rule_has_a_plain_language_explanation():
    ids = [r["id"] for r in yaml.safe_load(POLICY.read_text())["rules"]]
    assert ids, "no rules loaded"
    for rid in ids:
        text = explain_rule(rid, title="fallback")
        assert text.endswith(f"[{rid}]."), rid
        assert "fallback" not in text, f"{rid} has no explanation of its own"


def test_explanations_use_the_deal_numbers():
    assert explain_rule("R1b", discount_percent=19) == \
        "A 19% discount is above the 15% an AE can approve alone, so Deal Desk must sign off [R1b]."
    assert explain_rule("R3", payment_terms="net_60") == \
        "Net-60 payment terms are longer than the standard Net-30, so Finance must sign off [R3]."
    assert explain_rule("R11", net_total_cents=120_000_000).startswith("The net contract value is $1,200,000, over the $1,000,000 line")
    assert explain_rule("R2", term_months=36).startswith("This is a 36-month deal paid up front")
