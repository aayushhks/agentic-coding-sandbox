"""The fleet's records, as the store returns them and the api serves them."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fleet.policy import ExecutionPolicy

JobState = Literal[
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "escalated",
    "dead_lettered",
    "cancelled",
]
Outcome = Literal["succeeded", "failed", "escalated"]
UNFINISHED_STATES: frozenset[str] = frozenset({"queued", "claimed", "running"})
# the first run plus two retries after infrastructure failures
DEFAULT_MAX_ATTEMPTS = 3


class RetryPolicy(BaseModel):
    """The wait before a retry after an infrastructure failure: doubling from a base, to a cap."""

    model_config = ConfigDict(frozen=True)

    backoff_seconds: float = Field(default=2.0, ge=0)
    backoff_cap_seconds: float = Field(default=60.0, ge=0)


DEFAULT_RETRY = RetryPolicy()


class NewJob(BaseModel):
    name: str = Field(min_length=1)
    payload: dict[str, Any]


class Submission(BaseModel):
    batch_id: int
    job_ids: list[int]
    # false when this repeats an earlier submission with the same key and content
    created: bool


class ClaimedJob(BaseModel):
    id: int
    attempt: int
    name: str
    payload: dict[str, Any]
    lease_expires_at: datetime
    policy: ExecutionPolicy


class JobStatus(BaseModel):
    id: int
    batch_id: int
    position: int
    name: str
    state: JobState
    attempt: int
    max_attempts: int
    worker_id: str | None
    lease_expires_at: datetime | None
    result_id: int | None
    submitted_at: datetime
    # a job given back after a failure waits out its backoff before it can be claimed again
    available_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    # why the latest attempt ended without a result, and so why a dead letter is one
    last_error: str | None
    policy: ExecutionPolicy
    cancel_requested_at: datetime | None


class CancelResult(BaseModel):
    job_id: int
    # cancelled already, or still running until its worker notices the request
    state: JobState


class BatchStatus(BaseModel):
    batch_id: int
    label: str
    total: int
    counts: dict[str, int]
    done: bool


class AttemptRecord(BaseModel):
    """One claim of a job: who held it, for how long, and how it ended."""

    attempt: int
    worker_id: str
    claimed_at: datetime
    ended_at: datetime | None
    # published, lease_expired, released or cancelled; None while it still runs
    ended_by: str | None
    error: str | None


class PublishedResult(BaseModel):
    job_id: int
    attempt: int
    worker_id: str
    published_at: datetime
    outcome: Outcome
    body: dict[str, Any]
