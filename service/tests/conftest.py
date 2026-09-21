"""Every test runs against a throwaway database dg_test built from the real migrations."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PG_DB", "dg_test")
os.environ["DG_TOOL_TOKEN"] = "test-tool-token"
os.environ["DG_N8N_SHARED_SECRET"] = "test-n8n-secret"
os.environ["N8N_WEBHOOK_BASE"] = "http://127.0.0.1:9/webhook"  # nothing listens; dispatch fails fast
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DG_DEFAULT_REQUESTER_SLACK_ID", None)

from policy.evaluate import load_policy  # noqa: E402
from service.app.config import get_settings  # noqa: E402
from service.app.db import close_pool, init_pool, tx  # noqa: E402

get_settings.cache_clear()
SETTINGS = get_settings()
assert SETTINGS.pg_db == "dg_test"

EMPLOYEES = [
    # email, name, title, roles, manager_email, slack
    ("rafael@oriel.test", "Rafael Quintero", "Chief Revenue Officer", ["cro"], None, "U_RAFAEL"),
    ("noor@oriel.test", "Noor El-Amin", "Director, Enterprise Sales", ["ae"], "rafael@oriel.test", "U_NOOR"),
    ("maren@oriel.test", "Maren Holloway", "Enterprise AE", ["ae"], "noor@oriel.test", "U_MAREN"),
    ("priya@oriel.test", "Priya Ramaswamy", "Deal Desk Lead", ["deal_desk"], "rafael@oriel.test", "U_PRIYA"),
    ("dana@oriel.test", "Dana Okafor", "Revenue Controller", ["finance"], "rafael@oriel.test", "U_DANA"),
    ("silas@oriel.test", "Silas Wren", "Commercial Counsel", ["legal"], "rafael@oriel.test", "U_SILAS"),
    ("ingrid@oriel.test", "Ingrid Baptiste", "Head of Product", ["product"], "rafael@oriel.test", "U_INGRID"),
]


def _admin(dbname: str):
    return psycopg.connect(SETTINGS.dsn("admin").rsplit("/", 1)[0] + f"/{dbname}", autocommit=True, row_factory=dict_row)


@pytest.fixture(scope="session", autouse=True)
def database():
    with _admin("postgres") as conn:
        conn.execute("DROP DATABASE IF EXISTS dg_test WITH (FORCE)")
        conn.execute("CREATE DATABASE dg_test")
    with _admin("dg_test") as conn:
        conn.execute(f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dg_service') THEN
              CREATE ROLE dg_service LOGIN PASSWORD '{SETTINGS.pg_service_password}'; END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dg_reader') THEN
              CREATE ROLE dg_reader LOGIN PASSWORD '{SETTINGS.pg_reader_password}'; END IF;
            END $$;""")
        for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
            conn.execute(f.read_text().replace("ON DATABASE dg ", "ON DATABASE dg_test "))
        ids: dict[str, int] = {}
        for email, name, title, roles, _, slack in EMPLOYEES:
            row = conn.execute(
                "INSERT INTO employees (full_name, title, email, slack_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
                (name, title, email, slack)).fetchone()
            ids[email] = row["id"]
            for r in roles:
                conn.execute("INSERT INTO employee_roles (employee_id, role) VALUES (%s,%s)", (ids[email], r))
        for email, *_rest in EMPLOYEES:
            mgr = _rest[3]
            if mgr:
                conn.execute("UPDATE employees SET manager_id=%s WHERE id=%s", (ids[mgr], ids[email]))
    init_pool(SETTINGS.dsn("service"))
    policy = load_policy(SETTINGS.policy_path)
    with tx() as cur:
        cur.execute("INSERT INTO policy_versions (policy_version, version_label, yaml_text, rule_ids) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (policy.version, policy.version_label, policy.yaml_text, policy.rule_ids))
    yield
    close_pool()


@pytest.fixture(autouse=True)
def clean_tables():
    from service.app import db as _db

    if _db._pool is None:  # the app lifespan closes the pool on TestClient exit; reopen for core tests
        init_pool(SETTINGS.dsn("service"))
    with _admin("dg_test") as conn:
        conn.execute("TRUNCATE audit_events, notifications, approvals, approval_requirements, request_extractions, "
                     "deal_terms, conversations, deals RESTART IDENTITY CASCADE")
        conn.execute("ALTER SEQUENCE deal_number_seq RESTART WITH 1001")
        conn.execute("ALTER SEQUENCE audit_events_seq_seq RESTART WITH 1")
    yield


@pytest.fixture
def policy():
    return load_policy(SETTINGS.policy_path)


@pytest.fixture
def settings():
    return SETTINGS


@pytest.fixture
def admin():
    with _admin("dg_test") as conn:
        yield conn


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from service.app.main import app

    with TestClient(app) as c:
        yield c
