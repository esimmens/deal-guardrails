"""Properties that must hold for every possible deal, not just the table."""
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from policy.evaluate import evaluate, load_policy, merge_required, merge_terms
from policy.schema import FORBIDDEN_APPROVERS, REVIEWER_ROLES, TERM_FIELDS, Facts, PaymentTerms, Segment

POLICY = load_policy(Path(__file__).resolve().parents[1] / "pricing_policy.yaml")

facts_st = st.builds(
    Facts,
    discount_bps=st.integers(0, 10_000),
    term_months=st.integers(1, 72),
    payment_terms=st.sampled_from(list(PaymentTerms)),
    segment=st.sampled_from(list(Segment)),
    net_total_cents=st.integers(0, 500_000_000),
    **{name: st.booleans() for name in TERM_FIELDS},
)


@settings(max_examples=400)
@given(facts_st)
def test_required_is_always_a_set_of_reviewer_roles(facts):
    d = evaluate(facts, POLICY)
    assert set(d["required"]) <= REVIEWER_ROLES
    assert "ae" not in d["required"]
    assert not (set(d["required"]) & FORBIDDEN_APPROVERS)
    assert len(d["required"]) == len(set(d["required"]))


@settings(max_examples=400)
@given(facts_st)
def test_above_ae_band_always_has_a_reviewer(facts):
    d = evaluate(facts, POLICY)
    if facts.discount_bps > 1500:
        assert "deal_desk" in d["required"]


@settings(max_examples=400)
@given(facts_st)
def test_unresolved_always_escalates_to_deal_desk(facts):
    d = evaluate(facts, POLICY)
    if d["unresolved"]:
        assert "deal_desk" in d["required"]


@settings(max_examples=300)
@given(facts_st)
def test_gate_is_highest_ranked_required(facts):
    d = evaluate(facts, POLICY)
    if d["required"]:
        assert d["gate_role"] == max(d["required"], key=lambda r: POLICY.role_rank[r])
        assert d["required"] == sorted(d["required"], key=lambda r: (-POLICY.role_rank[r], r))
    else:
        assert d["gate_role"] is None


@settings(max_examples=300)
@given(facts_st, st.sampled_from(TERM_FIELDS))
def test_turning_any_term_on_never_shrinks_required(facts, term):
    before = set(evaluate(facts, POLICY)["required"])
    flipped = Facts(**{**facts.__dict__, term: True})
    after = set(evaluate(flipped, POLICY)["required"])
    assert before <= after


@settings(max_examples=200)
@given(facts_st)
def test_deterministic(facts):
    assert evaluate(facts, POLICY) == evaluate(facts, POLICY)


@settings(max_examples=300)
@given(
    st.fixed_dictionaries({k: st.one_of(st.none(), st.booleans()) for k in TERM_FIELDS}),
    st.fixed_dictionaries({k: st.one_of(st.none(), st.booleans()) for k in TERM_FIELDS}),
)
def test_merge_terms_only_escalates(llm, confirmed):
    merged, flags = merge_terms(llm, confirmed)
    for k in TERM_FIELDS:
        if llm.get(k) is True:
            assert merged[k] is True
            if confirmed.get(k) is False:
                assert f"human_downgrade_blocked:{k}" in flags
        elif confirmed.get(k) is not None:
            assert merged[k] is confirmed[k]
        else:
            assert merged[k] is False


@settings(max_examples=300)
@given(
    st.lists(st.sampled_from(sorted(REVIEWER_ROLES)), unique=True),
    st.lists(st.sampled_from(sorted(REVIEWER_ROLES) + ["ae", "bot", "nonsense"]), unique=True),
)
def test_merge_required_never_removes_and_never_adds_ae(engine, claimed):
    required, added = merge_required(engine, claimed, POLICY)
    assert set(engine) <= set(required)
    assert "ae" not in required and "bot" not in required and "nonsense" not in required
    assert set(added) == (set(claimed) & REVIEWER_ROLES) - set(engine)
