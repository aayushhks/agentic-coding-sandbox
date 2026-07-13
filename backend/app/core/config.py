from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root from backend/app/core/config.py; the committed ticket-eval report lives under docs/
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DEPLOYMENT_REPORT = _REPO_ROOT / "docs" / "results" / "tickets-reference.json"


def normalize_database_url(url: str) -> str:
    """Coerce a sync Postgres URL to the async (asyncpg) driver.

    Managed hosts (Heroku, Render, …) hand out ``postgresql://`` / ``postgres://`` URLs, but
    the app uses SQLAlchemy's async engine, which needs the ``+asyncpg`` driver. SQLite and
    already-async URLs are left untouched.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    """Application settings loaded from environment variables (and an optional .env file)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "agentic-coding-sandbox"
    environment: str = "development"
    log_level: str = "INFO"

    # llm provider selection: "groq" for real runs, "mock" for tests and local development
    llm_provider: str = "mock"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    # how the agent reaches its sandbox tools: "in_process" executes them directly (the default,
    # used by the benchmark and every test); "mcp" routes them through the sandbox MCP server over
    # stdio. Both paths go through the same Sandbox interface, so agent behavior is identical.
    tool_transport: str = "in_process"

    # database connection string, used from m5 onward
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_sandbox"

    # comma-separated CORS origins allowed to call the API ("*" allows any); needed when the
    # dashboard is served from a different origin than the API (e.g. a separately hosted frontend).
    cors_origins: str = "*"

    # path to the ticket-eval JSON report served to the deployment dashboard; defaults to the
    # committed reference report so the stakeholder view renders without a fresh eval run.
    deployment_report_path: str = str(_DEFAULT_DEPLOYMENT_REPORT)

    @field_validator("database_url")
    @classmethod
    def _coerce_async_driver(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is read only once."""
    return Settings()
