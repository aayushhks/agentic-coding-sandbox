from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is read only once."""
    return Settings()
