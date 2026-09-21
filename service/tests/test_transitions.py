import pytest

from service.app.transitions import TRANSITIONS, InvalidTransition, apply_transition


def test_code_and_database_agree(admin):
    rows = admin.execute("SELECT from_status::text f, event, to_status::text t FROM deal_transitions").fetchall()
    assert {(r["f"], r["event"]): r["t"] for r in rows} == TRANSITIONS


def test_illegal_transitions_raise():
    with pytest.raises(InvalidTransition):
        apply_transition("approved", "approve")
    with pytest.raises(InvalidTransition):
        apply_transition("cleared", "reject")
    assert apply_transition("pending_approval", "approve") == "approved"
