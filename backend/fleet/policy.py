"""The execution policy model: what a task may use, and the ceilings the operator allows.

Everything is denied unless granted. A batch asks for a policy when it is submitted; the operator's
limits decide whether it may have it; the worker enforces it through the container runtime.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# host:port, the only form of network access a task can be granted
_DESTINATION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?:(\d{1,5})$")


class PolicyError(ValueError):
    """A requested policy asks for more than the operator allows."""


def _destination(value: str) -> str:
    match = _DESTINATION.match(value)
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        raise ValueError(f"{value!r} is not a host:port destination")
    return value.lower()


class ExecutionPolicy(BaseModel):
    """What each attempt of a job runs with: cpu, memory, processes, scratch space, time, egress."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpus: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=1024, ge=64)
    pids: int = Field(default=256, ge=16)
    # the size of /tmp, the only writable scratch space, which holds the agent's workspace
    tmp_mb: int = Field(default=512, ge=16)
    timeout_seconds: float = Field(default=600.0, gt=0)
    # host:port destinations the task may reach through the egress proxy; none by default
    egress: tuple[str, ...] = ()

    @field_validator("egress")
    @classmethod
    def _destinations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({_destination(item) for item in value}))


DEFAULT_POLICY = ExecutionPolicy()


class OperatorLimits(BaseModel):
    """The most any batch may ask for, and the only destinations that may ever be granted."""

    model_config = ConfigDict(frozen=True)

    max_cpus: float = 2.0
    max_memory_mb: int = 4096
    max_pids: int = 1024
    max_tmp_mb: int = 2048
    max_timeout_seconds: float = 3600.0
    grantable_egress: frozenset[str] = frozenset()

    def check(self, policy: ExecutionPolicy) -> None:
        """Refuse a policy that goes past any ceiling, naming every one it breaks."""
        over = [
            f"{name} {asked} is over the limit of {limit}"
            for name, asked, limit in (
                ("cpus", policy.cpus, self.max_cpus),
                ("memory_mb", policy.memory_mb, self.max_memory_mb),
                ("pids", policy.pids, self.max_pids),
                ("tmp_mb", policy.tmp_mb, self.max_tmp_mb),
                ("timeout_seconds", policy.timeout_seconds, self.max_timeout_seconds),
            )
            if asked > limit
        ]
        over += [
            f"egress to {destination} is not grantable"
            for destination in policy.egress
            if destination not in self.grantable_egress
        ]
        if over:
            raise PolicyError("; ".join(over))
