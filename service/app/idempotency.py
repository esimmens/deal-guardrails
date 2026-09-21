"""One key per (conversation, business facts). Same facts in the same conversation is the
same request, even if the agent retries after a timeout. Different facts is a new request."""
from __future__ import annotations

import dataclasses
import hashlib

from policy.schema import TERM_FIELDS, Facts

from .audit import canonical_json


def canonical_digest(facts: Facts, account_normalized: str, list_total_cents: int) -> str:
    fd = dataclasses.asdict(facts)
    body = {
        "account": account_normalized,
        "list_total_cents": int(list_total_cents),
        "discount_bps": int(fd["discount_bps"]),
        "term_months": int(fd["term_months"]),
        "payment_terms": str(fd["payment_terms"]),
        "segment": str(fd["segment"]),
        "terms": {k: bool(fd[k]) for k in TERM_FIELDS},
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def idempotency_key(conversation_id: str | None, digest: str) -> str:
    return hashlib.sha256(f"{conversation_id or ''}|{digest}".encode("utf-8")).hexdigest()


def normalize_account_name(name: str) -> str:
    n = " ".join(name.strip().lower().split())
    for suffix in (" inc.", " inc", " llc", " ltd.", " ltd", " plc", " corp.", " corp", " co.", " gmbh"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].rstrip(" ,")
    return n
