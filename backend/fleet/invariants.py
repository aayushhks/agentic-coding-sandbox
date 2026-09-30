"""Check a drained run against the fleet's invariants, using only what the database recorded."""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

Invariant = Literal["one_result", "nothing_lost", "no_stale_write", "accounting"]
RESULT_STATES = frozenset({"succeeded", "failed", "escalated"})
FINAL_STATES = RESULT_STATES | {"dead_lettered", "cancelled"}
# how an attempt ends when its job has to be retried
RETRY_ENDINGS = frozenset({"lease_expired", "released"})
# endings a worker writes itself, which it can only do while its lease is live
LIVE_ENDINGS = frozenset({"released", "cancelled"})


class JobRow(BaseModel):
    id: int
    state: str
    attempt: int
    max_attempts: int
    result_id: int | None
    cancel_requested_at: datetime | None = None


class AttemptRow(BaseModel):
    job_id: int
    attempt: int
    worker_id: str
    claimed_at: datetime
    lease_expires_at: datetime
    ended_at: datetime | None
    ended_by: str | None


class ResultRow(BaseModel):
    id: int
    job_id: int
    attempt: int
    worker_id: str
    published_at: datetime
    outcome: str


class Snapshot(BaseModel):
    jobs: list[JobRow]
    attempts: list[AttemptRow]
    results: list[ResultRow]


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: Invariant
    job_id: int | None
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant} (job {self.job_id}): {self.detail}"


async def snapshot(engine: AsyncEngine, job_ids: Sequence[int]) -> Snapshot:
    """The jobs, their attempts and their results, read in one consistent view."""
    ids = {"ids": list(job_ids)}
    async with engine.connect() as connection:
        consistent = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with consistent.begin():
            jobs = await consistent.execute(
                text(
                    "select id, state, attempt, max_attempts, result_id, cancel_requested_at "
                    "from fleet_jobs where id = any(:ids)"
                ),
                ids,
            )
            attempts = await consistent.execute(
                text(
                    "select job_id, attempt, worker_id, claimed_at, lease_expires_at, ended_at, "
                    "ended_by from fleet_attempts where job_id = any(:ids)"
                ),
                ids,
            )
            results = await consistent.execute(
                text(
                    "select id, job_id, attempt, worker_id, published_at, outcome "
                    "from fleet_results where job_id = any(:ids)"
                ),
                ids,
            )
            return Snapshot(
                jobs=[JobRow.model_validate(row._asdict()) for row in jobs],
                attempts=[AttemptRow.model_validate(row._asdict()) for row in attempts],
                results=[ResultRow.model_validate(row._asdict()) for row in results],
            )


def check(run: Snapshot, submitted: Sequence[int]) -> list[Violation]:
    """Every broken invariant in a drained run; an empty list means all four hold."""
    jobs = {job.id: job for job in run.jobs}
    logs: dict[int, list[AttemptRow]] = defaultdict(list)
    for attempt in sorted(run.attempts, key=lambda row: row.attempt):
        logs[attempt.job_id].append(attempt)
    results: dict[int, list[ResultRow]] = defaultdict(list)
    for result in run.results:
        results[result.job_id].append(result)
    return [
        *_nothing_lost(jobs, submitted),
        *(
            violation
            for job in jobs.values()
            for violation in (
                *_one_result(job, results[job.id]),
                *_no_stale_write(job, logs[job.id], results[job.id]),
                *_accounting(job, logs[job.id]),
            )
        ),
        *_totals(jobs, submitted),
    ]


def _nothing_lost(jobs: dict[int, JobRow], submitted: Sequence[int]) -> list[Violation]:
    missing = [
        Violation("nothing_lost", job_id, "submitted but not in the store")
        for job_id in submitted
        if job_id not in jobs
    ]
    unfinished = [
        Violation("nothing_lost", job.id, f"left {job.state}")
        for job in jobs.values()
        if job.state not in FINAL_STATES
    ]
    return missing + unfinished


def _one_result(job: JobRow, results: list[ResultRow]) -> list[Violation]:
    found = []
    if job.state not in RESULT_STATES:
        if results:
            found.append(f"{job.state}, yet it has a result")
    elif len(results) != 1:
        found.append(f"{job.state} with {len(results)} results")
    elif results[0].id != job.result_id:
        found.append(f"points at result {job.result_id}, not its own {results[0].id}")
    elif results[0].outcome != job.state:
        found.append(f"{job.state}, but its result says {results[0].outcome}")
    return [Violation("one_result", job.id, detail) for detail in found]


def _no_stale_write(
    job: JobRow, log: list[AttemptRow], results: list[ResultRow]
) -> list[Violation]:
    found = []
    attempts = {attempt.attempt: attempt for attempt in log}
    for result in results:
        owner = attempts.get(result.attempt)
        if result.attempt != job.attempt:
            found.append(f"published by attempt {result.attempt}, but the last is {job.attempt}")
        if owner is None:
            found.append(f"published by attempt {result.attempt}, which has no record")
            continue
        if result.worker_id != owner.worker_id:
            found.append(f"published by {result.worker_id}, but {owner.worker_id} held it")
        if result.published_at >= owner.lease_expires_at:
            found.append(f"published after attempt {owner.attempt}'s lease ran out")
        if (owner.ended_by, owner.ended_at) != ("published", result.published_at):
            found.append(f"attempt {owner.attempt} was not ended by its own publish")
        if job.cancel_requested_at is not None and result.published_at >= job.cancel_requested_at:
            found.append("published after its cancel was requested")
    # a stale write needs two live attempts at once, so each must start after the last one ended
    for earlier, later in pairwise(log):
        if earlier.ended_at is None or later.claimed_at < earlier.ended_at:
            found.append(f"attempt {later.attempt} claimed while {earlier.attempt} still held it")
    return [Violation("no_stale_write", job.id, detail) for detail in found]


def _accounting(job: JobRow, log: list[AttemptRow]) -> list[Violation]:
    found = []
    numbers = [attempt.attempt for attempt in log]
    if numbers != list(range(1, job.attempt + 1)):
        found.append(f"attempt log {numbers} for a job on attempt {job.attempt}")
    if job.attempt > job.max_attempts:
        found.append(f"{job.attempt} attempts, over its budget of {job.max_attempts}")
    for attempt in log:
        if attempt.ended_at is None:
            found.append(f"attempt {attempt.attempt} never ended")
        elif attempt.ended_by == "lease_expired" and attempt.ended_at < attempt.lease_expires_at:
            found.append(f"attempt {attempt.attempt} was reaped before its lease ran out")
        elif attempt.ended_by in LIVE_ENDINGS and attempt.ended_at >= attempt.lease_expires_at:
            found.append(
                f"attempt {attempt.attempt} ended {attempt.ended_by} after its lease ran out"
            )
    found += [
        f"attempt {attempt.attempt} ended {attempt.ended_by}, yet the job ran again"
        for attempt in log[:-1]
        if attempt.ended_by not in RETRY_ENDINGS
    ]
    last = log[-1].ended_by if log else None
    if job.state in RESULT_STATES and last != "published":
        found.append(f"{job.state}, but its last attempt ended {last}")
    if job.state == "dead_lettered":
        if job.attempt < job.max_attempts:
            found.append(f"dead-lettered after {job.attempt} of {job.max_attempts} attempts")
        if last not in RETRY_ENDINGS:
            found.append(f"dead-lettered, but its last attempt ended {last}")
        if job.cancel_requested_at is not None:
            found.append("dead-lettered though its cancel was requested")
    if job.state == "cancelled":
        if job.cancel_requested_at is None:
            found.append("cancelled without a cancel request")
        if log and last not in {*RETRY_ENDINGS, "cancelled"}:
            found.append(f"cancelled, but its last attempt ended {last}")
    elif last == "cancelled":
        found.append(f"{job.state}, but its last attempt ended cancelled")
    return [Violation("accounting", job.id, detail) for detail in found]


def _totals(jobs: dict[int, JobRow], submitted: Sequence[int]) -> list[Violation]:
    counts = Counter(job.state for job in jobs.values())
    final = sum(counts[state] for state in FINAL_STATES)
    if final == len(submitted) == len(jobs):
        return []
    detail = f"{len(submitted)} submitted, {final} in a final state: {dict(sorted(counts.items()))}"
    return [Violation("accounting", None, detail)]
