"""The fleet's records, as the store returns them and the api serves them."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

JobState = Literal["queued", "claimed", "running", "succeeded", "failed", "escalated", "cancelled"]
Outcome = Literal["succeeded", "failed", "escalated"]
UNFINISHED_STATES: frozenset[str] = frozenset({"queued", "claimed", "running"})


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


class JobStatus(BaseModel):
    id: int
    batch_id: int
    position: int
    name: str
    state: JobState
    attempt: int
    worker_id: str | None
    lease_expires_at: datetime | None
    result_id: int | None
    submitted_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class BatchStatus(BaseModel):
    batch_id: int
    label: str
    total: int
    counts: dict[str, int]
    done: bool


class PublishedResult(BaseModel):
    job_id: int
    attempt: int
    worker_id: str
    published_at: datetime
    outcome: Outcome
    body: dict[str, Any]
