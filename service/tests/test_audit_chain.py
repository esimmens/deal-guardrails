import psycopg
import pytest

from service.app.core import ApprovalEvent, record_decision, submit_deal
from service.app.db import tx
from service.verify_audit import main as verify_main

from .helpers import ACTOR, submission


def _verify(settings) -> int:
    import sys
    argv, sys.argv = sys.argv, ["verify_audit", "--dsn", settings.dsn("reader")]
    try:
        return verify_main()
    finally:
        sys.argv = argv


def test_chain_verifies_after_real_activity(policy, settings, capsys):
    with tx() as cur:
        r = submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with tx() as cur:
        record_decision(cur, ApprovalEvent(deal_id=r.deal_ref, slack_user_id="U_PRIYA", decision="approve"),
                        settings, actor={"type": "slack_user", "id": "U_PRIYA"})
    assert _verify(settings) == 0
    assert capsys.readouterr().out.startswith("OK n=3")


def test_service_role_cannot_update_or_delete_audit(policy, settings):
    with tx() as cur:
        submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx() as cur:
            cur.execute("UPDATE audit_events SET event_type='x' WHERE seq=1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx() as cur:
            cur.execute("DELETE FROM audit_events WHERE seq=1")


def test_trigger_blocks_admin_edits_and_chain_catches_forced_edits(policy, settings, admin, capsys):
    with tx() as cur:
        submit_deal(cur, submission(), policy, settings, actor=ACTOR)
    with pytest.raises(psycopg.errors.RaiseException):
        admin.execute("UPDATE audit_events SET event_type='forged' WHERE seq=1")
    admin.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable")
    admin.execute("""UPDATE audit_events SET payload = payload || '{"discount_bps": 500}'::jsonb WHERE seq = 1""")
    admin.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_events_immutable")
    assert _verify(settings) == 1
    assert "BREAK at seq=1" in capsys.readouterr().out
