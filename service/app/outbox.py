"""Notifications outbox. Rows are written in the same transaction as the state change and
delivered afterwards, with retries. Delivery goes to n8n, which owns Slack; n8n never owns
a decision."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from psycopg import Cursor
from psycopg.types.json import Jsonb

from .audit import canonical_json
from .config import Settings
from .db import tx
from .security import sign

MAX_ATTEMPTS = 6


def enqueue(cur: Cursor, *, deal_id: int, kind: str, target: dict[str, Any], payload: dict[str, Any]) -> int:
    cur.execute(
        "INSERT INTO notifications (deal_id, kind, target, payload) VALUES (%s, %s, %s, %s) RETURNING id",
        (deal_id, kind, Jsonb(target), Jsonb(payload)),
    )
    return int(cur.fetchone()["id"])


def _path_for(kind: str) -> str:
    return "/deal-card" if kind == "approval_card" else "/notify"


def dispatch_once(settings: Settings, client: httpx.Client | None = None, limit: int = 50) -> dict[str, int]:
    """Deliver due notifications. Safe to call from a background task and from a cron."""
    own_client = client is None
    client = client or httpx.Client(timeout=10.0)
    sent = failed = deferred = 0
    try:
        with tx() as cur:
            cur.execute(
                """SELECT id, deal_id, kind::text AS kind, target, payload, attempts
                   FROM notifications
                   WHERE status = 'pending' AND next_attempt_at <= now()
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
            for row in rows:
                body = canonical_json({
                    "notification_id": row["id"], "kind": row["kind"], "deal_id": row["deal_id"],
                    "target": row["target"], "payload": row["payload"],
                }).encode("utf-8")
                ts = str(int(time.time()))
                headers = {
                    "Content-Type": "application/json",
                    "X-DG-Timestamp": ts,
                    "X-DG-Signature": sign(settings.dg_n8n_shared_secret, ts, body),
                }
                url = settings.n8n_webhook_base.rstrip("/") + _path_for(row["kind"])
                try:
                    resp = client.post(url, content=body, headers=headers)
                    ok = 200 <= resp.status_code < 300
                    err = None if ok else f"HTTP {resp.status_code}: {resp.text[:300]}"
                except httpx.HTTPError as exc:
                    ok, err = False, f"{type(exc).__name__}: {exc}"[:300]
                if ok:
                    cur.execute("UPDATE notifications SET status='sent', sent_at=now(), attempts=attempts+1, last_error=NULL WHERE id=%s",
                                (row["id"],))
                    sent += 1
                else:
                    attempts = int(row["attempts"]) + 1
                    if attempts >= MAX_ATTEMPTS:
                        cur.execute("UPDATE notifications SET status='failed', attempts=%s, last_error=%s WHERE id=%s",
                                    (attempts, err, row["id"]))
                        failed += 1
                    else:
                        backoff = datetime.now(UTC) + timedelta(minutes=2 ** attempts)
                        cur.execute("UPDATE notifications SET attempts=%s, next_attempt_at=%s, last_error=%s WHERE id=%s",
                                    (attempts, backoff, err, row["id"]))
                        deferred += 1
    finally:
        if own_client:
            client.close()
    return {"sent": sent, "failed": failed, "deferred": deferred}
