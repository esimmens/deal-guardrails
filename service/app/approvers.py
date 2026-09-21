"""Who may approve. A Slack user id is a claim; the employees table is the fact."""
from __future__ import annotations

from typing import Any

from psycopg import Cursor


def employee_by_slack(cur: Cursor, slack_user_id: str | None) -> dict[str, Any] | None:
    if not slack_user_id:
        return None
    cur.execute("SELECT * FROM employees WHERE slack_user_id = %s AND is_active", (slack_user_id,))
    return cur.fetchone()


def employee_by_id(cur: Cursor, employee_id: int) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM employees WHERE id = %s", (employee_id,))
    return cur.fetchone()


def roles_of(cur: Cursor, employee_id: int) -> set[str]:
    cur.execute("SELECT role::text AS role FROM employee_roles WHERE employee_id = %s", (employee_id,))
    return {r["role"] for r in cur.fetchall()}


def holders_of(cur: Cursor, role: str) -> list[dict[str, Any]]:
    cur.execute(
        """SELECT e.* FROM employees e JOIN employee_roles r ON r.employee_id = e.id
           WHERE r.role = %s AND e.is_active ORDER BY e.id""",
        (role,),
    )
    return cur.fetchall()


def gate_holders(cur: Cursor, role: str, requester: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Everyone who holds the gate role except the requester. If that leaves nobody, the
    requester's manager is designated (skip-level) and a flag says so."""
    flags: list[str] = []
    holders = [h for h in holders_of(cur, role) if h["id"] != requester["id"]]
    if holders:
        return holders, flags
    if requester.get("manager_id"):
        mgr = employee_by_id(cur, requester["manager_id"])
        if mgr and mgr["is_active"]:
            flags.append("skip_level")
            return [mgr], flags
    flags.append("no_gate_holder")
    return [], flags
