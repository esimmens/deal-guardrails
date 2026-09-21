"""Named fixtures the write-up and the demo point at."""
from pathlib import Path

import pytest

from policy.evaluate import PolicyError, evaluate, load_policy, parse_requires_approvals
from policy.schema import Facts

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(ROOT / "pricing_policy.yaml")


def test_the_calloway_case_is_unresolved_not_deescalated():
    """22% on 36 months annual prepaid: the multi-year allowance has no stated size."""
    d = evaluate(Facts(discount_bps=2200, term_months=36, payment_terms="annual_prepaid",
                       segment="enterprise", net_total_cents=60_000_000), POLICY)
    assert d["required"] == ["deal_desk"]
    assert d["unresolved"] == ["R2"]
    assert "within_ae_authority" not in d["flags"]
    note = next(r["note"] for r in d["rules_fired"] if r["id"] == "R2")
    assert "does not state" in note


def test_the_aha_case_discount_fine_terms_not():
    """12% is inside AE authority. Termination for convenience is not. It escalates anyway."""
    d = evaluate(Facts(discount_bps=1200, term_months=12, payment_terms="net_30", segment="mid_market",
                       net_total_cents=9_500_000, termination_for_convenience=True), POLICY)
    assert "within_ae_authority" in d["flags"]
    assert d["required"] == ["legal", "deal_desk"]
    assert d["gate_role"] == "legal"


def test_policy_version_is_content_hash():
    again = load_policy(ROOT / "pricing_policy.yaml")
    assert POLICY.version == again.version
    assert POLICY.version.startswith("sha256:") and len(POLICY.version) == len("sha256:") + 64


def test_parse_requires_approvals_is_forgiving_and_safe():
    assert parse_requires_approvals("deal_desk, Finance ;legal") == ["deal_desk", "finance", "legal"]
    assert parse_requires_approvals("ae, bot, AI, none") == []
    assert parse_requires_approvals(None) == []
    assert parse_requires_approvals("deal desk") == ["deal_desk"]


def test_unknown_role_in_yaml_is_rejected(tmp_path):
    bad = (ROOT / "pricing_policy.yaml").read_text().replace("require: [finance]", "require: [bot]", 1)
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(PolicyError):
        load_policy(p)


def test_prose_policy_cites_every_rule_id():
    prose = ROOT / "deal_approval_policy.md"
    if not prose.exists():
        pytest.skip("prose policy not written yet")
    text = prose.read_text()
    missing = [rid for rid in POLICY.rule_ids if rid not in text]
    assert not missing, f"prose policy never cites: {missing}"
