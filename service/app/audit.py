"""Append-only, hash-chained audit log.

Every state change writes its audit row in the same transaction. The row stores the exact
canonical bytes that were hashed, so a verifier can re-derive them from the jsonb columns
and catch any later edit to payload, actor, type or time.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb

GENESIS = "0" * 64
LOCK_KEY = 4242


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_time(t: datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_row(seq: int, deal_id: int | None, event_type: str, actor: dict, payload: dict,
                  occurred_at: datetime) -> str:
    return canonical_json({
        "seq": seq, "deal_id": deal_id, "event_type": event_type, "actor": actor,
        "payload": payload, "occurred_at": canonical_time(occurred_at),
    })


def row_hash_of(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def append(cur: Cursor, *, deal_id: int | None, event_type: str, actor: dict, payload: dict,
           occurred_at: datetime | None = None) -> dict[str, Any]:
    """Serialises appends inside the transaction, links to the previous row, inserts."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
    cur.execute("SELECT row_hash FROM audit_events ORDER BY seq DESC LIMIT 1")
    row = cur.fetchone()
    prev = row["row_hash"] if row else GENESIS
    cur.execute("SELECT nextval('audit_events_seq_seq') AS seq")
    seq = int(cur.fetchone()["seq"])
    occurred_at = occurred_at or datetime.now(UTC)
    canonical = canonical_row(seq, deal_id, event_type, actor, payload, occurred_at)
    digest = row_hash_of(prev, canonical)
    cur.execute(
        """INSERT INTO audit_events (seq, deal_id, event_type, actor, payload, occurred_at, canonical, prev_hash, row_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (seq, deal_id, event_type, Jsonb(actor), Jsonb(payload), occurred_at, canonical, prev, digest),
    )
    return {"seq": seq, "row_hash": digest, "prev_hash": prev}


def head(cur: Cursor) -> dict[str, Any]:
    cur.execute("SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1")
    row = cur.fetchone()
    return {"seq": int(row["seq"]), "hash": row["row_hash"]} if row else {"seq": 0, "hash": GENESIS}
