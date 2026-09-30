"""Fleet settings, read from FLEET_* environment variables."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def async_url(url: str) -> str:
    """Point a plain Postgres URL at the asyncpg driver the fleet uses."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


class FleetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEET_", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_sandbox"
    # how long a claim lasts before another worker may take the job back; longer than any job
    # until heartbeats extend it
    lease_seconds: float = 600.0

    @field_validator("database_url")
    @classmethod
    def _asyncpg(cls, value: str) -> str:
        return async_url(value)
