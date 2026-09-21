#!/usr/bin/env python
"""Seed Oriel Speech.

Reference data (employees, accounts, price book) is upserted. The 41 historical deals are
written through the same submit/approve code path as live traffic, in timeline order, so
the audit chain is genuine. Historical notifications are marked sent so nothing is posted
to Slack on the next dispatch. Idempotent: re-running after `reset.sql` rebuilds the same
history; re-running without a reset changes nothing (idempotency keys dedupe).
"""
from __future__ import annotations

import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from psycopg.types.json import Jsonb  # noqa: F401  (imported for side effects in dict adaptation)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from policy.evaluate import load_policy  # noqa: E402
from service.app.config import get_settings  # noqa: E402
from service.app.core import ApprovalEvent, DealSubmission, Terms, record_decision, submit_deal, transition_deal  # noqa: E402
from service.app.db import init_pool, tx  # noqa: E402
from service.app.main import register_policy_version  # noqa: E402

HERE = Path(__file__).parent
SEED_ACTOR = {"type": "system", "id": "seed"}


def load(name: str):
    return yaml.safe_load((HERE / name).read_text())


def seed_reference(cur) -> dict[str, dict]:
    people = {}
    for e in load("employees.yaml"):
        slack = os.environ.get(e["slack_env"]) or e["slack_placeholder"]
        cur.execute(
            """INSERT INTO employees (full_name, title, email, slack_user_id) VALUES (%s, %s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name, title = EXCLUDED.title,
                 slack_user_id = EXCLUDED.slack_user_id, is_active = true RETURNING id""",
            (e["name"], e["title"], e["email"], slack),
        )
        e["id"] = cur.fetchone()["id"]
        e["slack"] = slack
        people[e["email"]] = e
        for r in e["roles"]:
            cur.execute("INSERT INTO employee_roles (employee_id, role) VALUES (%s, %s) ON CONFLICT DO NOTHING", (e["id"], r))
    for e in people.values():
        mgr = people[e["manager"]]["id"] if e.get("manager") else None
        cur.execute("UPDATE employees SET manager_id = %s WHERE id = %s", (mgr, e["id"]))
    for a in load("accounts.yaml"):
        from service.app.idempotency import normalize_account_name
        cur.execute(
            """INSERT INTO accounts (name, name_normalized, segment, is_strategic, region) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (name_normalized) DO UPDATE SET segment = EXCLUDED.segment,
                 is_strategic = EXCLUDED.is_strategic, region = EXCLUDED.region""",
            (a["name"], normalize_account_name(a["name"]), a["segment"], a["is_strategic"], a["region"]),
        )
    for p in load("price_book.yaml"):
        cur.execute(
            """INSERT INTO price_book (sku, name, unit, list_unit_price_micros, effective_from, is_assumption)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name,
                 list_unit_price_micros = EXCLUDED.list_unit_price_micros, is_assumption = EXCLUDED.is_assumption""",
            (p["sku"], p["name"], p["unit"], round(p["list_unit_price_usd"] * 1_000_000), "2026-01-01", p["is_assumption"]),
        )
    return people


# ---------------------------------------------------------------------------------------
# Historical deals: 41 rows, deterministic, with hand-authored rows at fixed positions.
# ---------------------------------------------------------------------------------------
AE_EMAILS = ["maren.holloway@orielspeech.example", "tobias.achterberg@orielspeech.example",
             "noor.el-amin@orielspeech.example"]
PHRASES = [
    "{acct}, {amt} list over {term} months, asking {disc}%{extra}.",
    "need approval on {acct}: {disc}% off, {term}-month term, {pay}{extra}",
    "{acct} wants {disc}%. {amt} total at list, {term} months, {pay}{extra}.",
    "Can I do {disc} on {acct}? {amt} over {term} months{extra}. {pay}.",
]
PAY_TEXT = {"net_30": "net-30", "net_45": "net-45", "net_60": "net-60", "net_90": "net-90",
            "annual_prepaid": "paid annually up front", "multi_year_prepaid": "full term prepaid"}


def _timeline(n: int, rng: random.Random) -> list[datetime]:
    start, end = datetime(2026, 3, 12, tzinfo=UTC), datetime(2026, 9, 18, tzinfo=UTC)
    out = set()
    while len(out) < n:
        t = start + timedelta(seconds=rng.randint(0, int((end - start).total_seconds())))
        if t.weekday() < 5:
            t = t.replace(hour=rng.choice([8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19]), minute=rng.randint(0, 59),
                          second=rng.randint(0, 59), microsecond=0)
            out.add(t)
    return sorted(out)


def _generated_row(i: int, rng: random.Random, accounts: list[dict]) -> dict:
    # discount distribution over 41: 14 low, 18 mid, 6 contested, 3 cro
    band = "low" if i % 3 == 0 else ("mid" if i % 7 not in (0, 4) else ("contested" if i % 14 != 0 else "cro"))
    disc = {"low": rng.choice([5, 8, 10, 12, 14, 15]), "mid": rng.choice([16, 18, 20, 22, 24, 25]),
            "contested": rng.choice([26, 27, 28, 29, 30]), "cro": rng.choice([32, 35, 40])}[band]
    acct = rng.choice(accounts)
    term = rng.choice([12, 12, 12, 24, 36])
    pay = rng.choices(["net_30", "annual_prepaid", "net_60", "net_45"], weights=[60, 25, 10, 5])[0]
    amt = rng.choice([24000, 36000, 48000, 60000, 85000, 120000, 150000, 210000, 275000, 340000, 420000, 600000])
    terms = {}
    extra = ""
    r = rng.random()
    if r < 0.08:
        terms["termination_for_convenience"] = True; extra = ", they want termination for convenience"
    elif r < 0.15:
        terms["nonstandard_legal_terms"] = True; extra = ", their paper for the DPA"
    elif r < 0.20:
        terms["outcome_based_pricing"] = True; extra = ", part of the fee tied to containment rate"
    elif r < 0.27:
        terms["implementation_arrangement"] = True; extra = ", implementation bundled in"
    elif r < 0.31:
        terms["license_fee_restructure"] = True; extra = ", moving the platform fee into usage"
    if rng.random() < 0.4:
        terms["is_renewal"] = True
    if rng.random() < 0.3:
        terms["is_competitive"] = True
    if acct["is_strategic"] and rng.random() < 0.5:
        terms["claims_strategic_account"] = True; extra += ", strategic account"
    text = rng.choice(PHRASES).format(acct=acct["name"], amt=f"{amt:,}", term=term, disc=disc, pay=PAY_TEXT[pay], extra=extra)
    return dict(requester=rng.choice(AE_EMAILS), account=acct["name"], amount_usd=amt, discount_percent=disc,
                term_months=term, payment_terms=pay, segment=acct["segment"], terms=terms,
                requires_approvals=None, raw_request_text=text, outcome=None, decided_after_hours=rng.randint(1, 72))


def build_history() -> list[dict]:
    rng = random.Random(11)
    accounts = load("accounts.yaml")
    notable = {n["position"]: n for n in load("notable_deals.yaml")}
    times = _timeline(41, rng)
    rows = []
    for pos in range(1, 42):
        row = dict(notable[pos]) if pos in notable else _generated_row(pos, rng, accounts)
        row["position"] = pos
        row["submitted_at"] = times[pos - 1]
        rows.append(row)
    # outcomes for generated rows: mostly approved, a few rejected/cancelled, two left pending, two cleared
    gen = [r for r in rows if r["outcome"] is None]
    for r in gen:
        r["outcome"] = "approved"
    for r in rng.sample(gen, 5):
        r["outcome"] = "rejected"
    for r in rng.sample([r for r in gen if r["outcome"] == "approved"], 3):
        r["outcome"] = "cancelled"
    for r in [r for r in gen if r["outcome"] == "approved"][-2:]:
        r["outcome"] = "pending"
    cleared = 0
    for r in gen:
        if r["outcome"] == "approved" and r["discount_percent"] <= 15 and r["payment_terms"] in ("net_30", "annual_prepaid") \
                and r["segment"] != "public_sector" and not any(v for k, v in r["terms"].items() if k not in ("is_renewal", "is_competitive")):
            r["outcome"] = "cleared"; cleared += 1
            if cleared == 2:
                break
    return rows


def _submission(row: dict, people: dict, conv: str) -> DealSubmission:
    return DealSubmission(
        conversation_id=conv, requester_slack_user_id=people[row["requester"]]["slack"], account_name=row["account"],
        amount_usd=row["amount_usd"], value_basis="list_total", discount_percent=row["discount_percent"],
        term_months=row["term_months"], payment_terms=row["payment_terms"], segment=row["segment"],
        terms=Terms(**row.get("terms", {})), requires_approvals=row.get("requires_approvals"),
        raw_request_text=row["raw_request_text"], llm_model="seed", agent_version="seed",
    )


def seed_history(policy, settings, people: dict) -> None:
    rows = build_history()
    # a global timeline of events so audit seq order matches occurred_at order
    events: list[tuple[datetime, int, str, dict]] = []
    for r in rows:
        events.append((r["submitted_at"], 0, "submit", r))
        if r["outcome"] in ("approved", "rejected", "cancelled", "superseded"):
            events.append((r["submitted_at"] + timedelta(hours=r.get("decided_after_hours", 12)), 1, r["outcome"], r))
    events.sort(key=lambda e: (e[0], e[1]))
    refs: dict[int, tuple[int, str]] = {}
    for when, _, kind, r in events:
        conv = f"seed-{r['position']:03d}"
        with tx() as cur:
            if kind == "submit":
                res = submit_deal(cur, _submission(r, people, conv), policy, settings, actor=SEED_ACTOR, occurred_at=when)
                refs[r["position"]] = (res.deal_id, res.deal_ref)
                if r["outcome"] == "cleared" and res.status != "cleared":
                    print(f"  note: position {r['position']} expected cleared but got {res.status}")
                continue
            deal_id, deal_ref = refs[r["position"]]
            cur.execute("SELECT status::text s, gate_employee_ids g FROM deals WHERE id = %s", (deal_id,))
            d = cur.fetchone()
            if d["s"] != "pending_approval":
                continue
            if kind in ("approved", "rejected"):
                cur.execute("SELECT slack_user_id FROM employees WHERE id = ANY(%s) ORDER BY id LIMIT 1", (d["g"],))
                approver = cur.fetchone()
                if not approver:
                    continue
                record_decision(cur, ApprovalEvent(deal_id=deal_ref, slack_user_id=approver["slack_user_id"],
                                                   decision="approve" if kind == "approved" else "reject"),
                                settings, actor={"type": "slack_user", "id": approver["slack_user_id"], "via": "seed"},
                                occurred_at=when)
            elif kind == "cancelled":
                transition_deal(cur, deal_id, "cancel", actor=SEED_ACTOR, occurred_at=when)
            elif kind == "superseded":
                new = {**r, **r["superseded_by"], "position": r["position"]}
                res = submit_deal(cur, _submission(new, people, conv + "-b"), policy, settings, actor=SEED_ACTOR,
                                  occurred_at=when)
                transition_deal(cur, deal_id, "supersede", actor=SEED_ACTOR, occurred_at=when, supersedes_with=res.deal_id)
    with tx() as cur:
        cur.execute("UPDATE notifications SET status = 'sent', sent_at = now() WHERE status = 'pending'")
        cur.execute("SELECT status::text s, count(*) n FROM deals GROUP BY 1 ORDER BY 1")
        print("  deals by status:", {r["s"]: r["n"] for r in cur.fetchall()})
        cur.execute("SELECT count(*) n FROM audit_events")
        print("  audit events:", cur.fetchone()["n"])


def main() -> None:
    settings = get_settings()
    policy = load_policy(settings.policy_path)
    init_pool(settings.dsn("service"))
    register_policy_version(policy)
    with tx() as cur:
        people = seed_reference(cur)
        cur.execute("SELECT count(*) n FROM deals")
        existing = cur.fetchone()["n"]
    print(f"reference data upserted ({len(people)} employees); existing deals: {existing}")
    if existing:
        print("deals already present; run `make reset` to rebuild history")
        return
    seed_history(policy, settings, people)
    print("history seeded")


if __name__ == "__main__":
    main()
