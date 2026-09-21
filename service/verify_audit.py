#!/usr/bin/env python
"""Walk the audit chain. Exit 0 if every link holds, 1 at the first break.

Checks per row: (1) prev_hash equals the previous row's row_hash, (2) the canonical bytes
re-derived from the jsonb columns equal the stored canonical, (3) sha256(prev + canonical)
equals row_hash. Reads as dg_reader: it can only look.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.app.audit import GENESIS, canonical_row, row_hash_of  # noqa: E402
from service.app.config import get_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", action="store_true", help="print only the current head")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    dsn = args.dsn or get_settings().dsn("reader")
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        if args.head:
            cur.execute("SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1")
            r = cur.fetchone()
            print(f"head seq={r['seq'] if r else 0} hash={r['row_hash'] if r else GENESIS}")
            return 0
        cur.execute("SELECT * FROM audit_events ORDER BY seq")
        prev, n, last = GENESIS, 0, None
        for row in cur:
            n += 1
            if row["prev_hash"] != prev:
                print(f"BREAK at seq={row['seq']}: prev_hash does not link to the previous row")
                return 1
            derived = canonical_row(int(row["seq"]), row["deal_id"], row["event_type"], row["actor"],
                                    row["payload"], row["occurred_at"])
            if derived != row["canonical"]:
                print(f"BREAK at seq={row['seq']}: payload/actor/type/time do not match the canonical bytes that were hashed")
                return 1
            if row_hash_of(prev, row["canonical"]) != row["row_hash"]:
                print(f"BREAK at seq={row['seq']}: row_hash does not match sha256(prev_hash + canonical)")
                return 1
            prev, last = row["row_hash"], row["seq"]
        print(f"OK n={n} head_seq={last or 0} head={prev[:16]}...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
