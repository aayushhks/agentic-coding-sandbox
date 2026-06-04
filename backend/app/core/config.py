from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Coerce a sync Postgres URL to the async (asyncpg) driver.

    Managed hosts (Railway, Heroku, …) hand out ``postgresql://`` / ``postgres://`` URLs, but
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

    # database connection string, used from m5 onward
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_sandbox"

    # comma-separated CORS origins allowed to call the API ("*" allows any); needed when the
    # dashboard is served from a different origin than the API (e.g. Vercel -> Railway).
    cors_origins: str = "*"

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
