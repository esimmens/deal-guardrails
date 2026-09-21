"""Settings. Reads environment variables, then .env or env in the repo root."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), str(REPO_ROOT / "env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres. Inside compose DATABASE_URL is set; locally we build it from the passwords.
    database_url: str | None = None
    database_url_reader: str | None = None
    database_url_admin: str | None = None
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "dg"
    pg_admin_password: str = "dg_admin_dev_pw"
    pg_service_password: str = "dg_service_dev_pw"
    pg_reader_password: str = "dg_reader_dev_pw"

    # Trust boundaries
    dg_tool_token: str = ""  # bearer the ElevenAgents tool sends
    dg_n8n_shared_secret: str = ""  # HMAC key, both directions with n8n
    hmac_window_seconds: int = 300

    # Neighbours
    n8n_webhook_base: str = "http://localhost:5678/webhook"
    public_base_url: str = "http://localhost:8000"

    # Policy
    policy_path: Path = REPO_ROOT / "policy" / "pricing_policy.yaml"

    # Evals: when a submission carries no requester id (the test harness cannot inject the
    # Slack integration variable), fall back to this employee and audit that we did.
    dg_default_requester_slack_id: str | None = None

    def dsn(self, role: str = "service") -> str:
        explicit = {"service": self.database_url, "reader": self.database_url_reader,
                    "admin": self.database_url_admin}[role]
        if explicit:
            return explicit
        user, pw = {
            "service": ("dg_service", self.pg_service_password),
            "reader": ("dg_reader", self.pg_reader_password),
            "admin": ("dg_admin", self.pg_admin_password),
        }[role]
        return f"postgresql://{user}:{pw}@{self.pg_host}:{self.pg_port}/{self.pg_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
