"""Fleet settings, read from FLEET_* environment variables."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fleet.models import DEFAULT_RETRY


def async_url(url: str) -> str:
    """Point a plain Postgres URL at the asyncpg driver the fleet uses."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


class FleetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEET_", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_sandbox"
    # how long a claim lasts without a heartbeat before another worker may take the job back
    lease_seconds: float = 30.0
    # how often a running job's lease is extended; a third of the lease when unset
    heartbeat_seconds: float | None = None
    # how often a busy worker returns lapsed leases to the queue; an idle one does it every poll
    reap_every_seconds: float = 5.0
    # the wait before retrying after an infrastructure failure, doubling up to the cap
    retry_backoff_seconds: float = DEFAULT_RETRY.backoff_seconds
    retry_backoff_cap_seconds: float = DEFAULT_RETRY.backoff_cap_seconds

    @field_validator("database_url")
    @classmethod
    def _asyncpg(cls, value: str) -> str:
        return async_url(value)
