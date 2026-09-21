"""Deal Guardrails service. Postgres is the only source of truth; this process is the
only writer; every request is one transaction."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from policy.evaluate import load_policy

from .config import get_settings
from .db import close_pool, init_pool, tx
from .routes import deals, events, health, notifications


def register_policy_version(policy) -> None:
    with tx() as cur:
        cur.execute(
            """INSERT INTO policy_versions (policy_version, version_label, yaml_text, rule_ids)
               VALUES (%s, %s, %s, %s) ON CONFLICT (policy_version) DO NOTHING""",
            (policy.version, policy.version_label, policy.yaml_text, policy.rule_ids),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.policy = load_policy(settings.policy_path)
    init_pool(settings.dsn("service"))
    register_policy_version(app.state.policy)
    try:
        yield
    finally:
        close_pool()


app = FastAPI(title="Deal Guardrails", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(deals.router)
app.include_router(events.router)
app.include_router(notifications.router)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # 400, not 422: nothing was recorded, so the agent's failure edge is the truthful one.
    return JSONResponse(status_code=400, content={"error": "validation", "detail": exc.errors()})
