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


class JobOutput(BaseModel):
    """What the agent left behind and what the grader made of it, cut to fit in a record."""

    answer: str
    escalation_reason: str
    # every file the agent added, changed or removed, and a unified diff of them
    files_changed: list[str]
    diff: str
    # the hidden tests' verdict, their exit code and the end of their output; on a ticket, also
    # whether the files the ticket must not touch were left alone
    tests: dict[str, Any]
    tools: dict[str, int]
    # the time the job waited on the model, over all its calls
    model_seconds: float


class AttemptRun(BaseModel):
    """One attempt at a job, its times in seconds since the batch was submitted."""

    attempt: int
    worker: str
    claimed_at: float
    ended_at: float | None
    # published, lease_expired, released or cancelled
    ended_by: str | None
    error: str | None


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
    # the models that answered and the builds they ran, as the provider reported them
    served_models: list[str] = []
    fingerprints: list[str] = []
    # what the job's tokens cost at the run's pinned price, None without one
    cost_usd: float | None = None
    output: JobOutput | None = None
    # every attempt the fleet made at the job, in order; None where there is no fleet
    attempt_history: list[AttemptRun] | None = None

    @property
    def queue_wait(self) -> float:
        return self.claimed_at - self.submitted_at

    @property
    def service_time(self) -> float:
        return self.finished_at - self.claimed_at

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
