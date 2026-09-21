"""The state machine, in code. db/migrations/004_transitions.sql holds the same table as
data and a test asserts they are identical. Everything not listed here is illegal."""
from __future__ import annotations

TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending_approval", "approve"): "approved",
    ("pending_approval", "reject"): "rejected",
    ("pending_approval", "cancel"): "cancelled",
    ("pending_approval", "supersede"): "superseded",
    ("pending_approval", "expire"): "expired",
}

TERMINAL = frozenset({"cleared", "approved", "rejected", "cancelled", "superseded", "expired"})


class InvalidTransition(Exception):
    def __init__(self, status: str, event: str) -> None:
        super().__init__(f"no transition from {status!r} on {event!r}")
        self.status = status
        self.event = event


def apply_transition(status: str, event: str) -> str:
    try:
        return TRANSITIONS[(status, event)]
    except KeyError:
        raise InvalidTransition(status, event) from None
