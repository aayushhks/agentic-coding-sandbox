"""What happened to one job in a batch."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class Outcome(StrEnum):
    SOLVED = "solved"
    ESCALATED = "escalated"
    FAILED = "failed"


class FailureKind(StrEnum):
    TASK = "task"  # the agent could not solve it
    INFRA = "infra"  # the model api or the sandbox failed underneath it
    HARNESS = "harness"  # replay could not reproduce the recording


class JobResult(BaseModel):
    job_id: str
    task_id: str
    repeat: int
    kind: str
    expected: str
    worker: str
    attempts: int
    # seconds since the batch was submitted
    submitted_at: float
    claimed_at: float
    finished_at: float
    outcome: Outcome
    failure_mode: str | None
    failure_kind: FailureKind | None
    matched_expectation: bool
    termination_reason: str
    iterations: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    retry_wait_seconds: float
    divergence: str | None
    # how the job's final attempt ran, when it ran in a container: limits, exit, peak memory
    execution: dict[str, Any] | None = None

    @property
    def queue_wait(self) -> float:
        return self.claimed_at - self.submitted_at

    @property
    def service_time(self) -> float:
        return self.finished_at - self.claimed_at

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
