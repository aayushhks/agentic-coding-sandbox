"""Fleet settings, read from FLEET_* environment variables."""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fleet.models import DEFAULT_RETRY
from fleet.policy import OperatorLimits

_LIMITS = OperatorLimits()


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
    # how long a worker keeps retrying a call whose database connection dropped, before it exits
    db_retry_seconds: float = 60.0
    # the wait before retrying after an infrastructure failure, doubling up to the cap
    retry_backoff_seconds: float = DEFAULT_RETRY.backoff_seconds
    retry_backoff_cap_seconds: float = DEFAULT_RETRY.backoff_cap_seconds
    # the operator's ceilings on what any batch may ask for
    max_cpus: float = _LIMITS.max_cpus
    max_memory_mb: int = _LIMITS.max_memory_mb
    max_pids: int = _LIMITS.max_pids
    max_tmp_mb: int = _LIMITS.max_tmp_mb
    max_timeout_seconds: float = _LIMITS.max_timeout_seconds
    # comma-separated host:port destinations a batch may be granted; none unless listed
    grantable_egress: str = ""
    # where attempts run: in the worker's process, or each in a container of its own
    execution: Literal["process", "container"] = "process"
    task_image: str = "fleet-task:local"
    docker_socket: str = "/var/run/docker.sock"
    # labels this fleet's containers, so one fleet never stops another's on a shared daemon
    deployment: str = "default"
    # the network egress proxies reach granted destinations through
    egress_network: str = "bridge"

    def operator_limits(self) -> OperatorLimits:
        return OperatorLimits(
            max_cpus=self.max_cpus,
            max_memory_mb=self.max_memory_mb,
            max_pids=self.max_pids,
            max_tmp_mb=self.max_tmp_mb,
            max_timeout_seconds=self.max_timeout_seconds,
            grantable_egress=frozenset(
                item.strip().lower() for item in self.grantable_egress.split(",") if item.strip()
            ),
        )

    @field_validator("database_url")
    @classmethod
    def _asyncpg(cls, value: str) -> str:
        return async_url(value)
