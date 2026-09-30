"""The fleet's job store: every state change is one transaction on Postgres's clock."""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY,
    UNFINISHED_STATES,
    BatchStatus,
    ClaimedJob,
    JobState,
    JobStatus,
    NewJob,
    Outcome,
    PublishedResult,
    RetryPolicy,
    Submission,
)
from fleet.policy import DEFAULT_POLICY, ExecutionPolicy

_JOB_COLUMNS = (
    "id, batch_id, position, name, state, attempt, max_attempts, worker_id, lease_expires_at, "
    "result_id, submitted_at, available_at, claimed_at, started_at, finished_at, last_error, "
    "policy, cancel_requested_at"
)
LEASE_EXPIRED = "lease expired"


def _end_attempts(chosen: str, ending: str) -> str:
    """End attempts without a result: requeue after a backoff, or cancel or dead-letter the job."""
    retry = "j.cancel_requested_at is null and j.attempt < j.max_attempts"
    final = "case when j.cancel_requested_at is not null then 'cancelled' else 'dead_lettered' end"
    backoff = (
        "least(CAST(:backoff AS double precision) * power(2, j.attempt - 1), "
        "CAST(:cap AS double precision))"
    )
    return (
        "with now as materialized (select clock_timestamp() as at), "
        f"chosen as ({chosen}), "
        "moved as ("
        "update fleet_jobs as j set "
        f"state = case when {retry} then 'queued' else {final} end, "
        f"available_at = case when {retry} "
        f"then now.at + make_interval(secs => {backoff}) else j.available_at end, "
        f"worker_id = case when {retry} then null else j.worker_id end, "
        f"claimed_at = case when {retry} then null else j.claimed_at end, "
        f"started_at = case when {retry} then null else j.started_at end, "
        f"finished_at = case when {retry} then null else now.at end, "
        "lease_expires_at = null, last_error = :error, updated_at = now.at "
        "from now, chosen where j.id = chosen.id "
        "returning j.id, j.attempt, j.state) "
        "update fleet_attempts as a set "
        f"ended_at = now.at, ended_by = '{ending}', error = :error "
        "from moved, now where a.job_id = moved.id and a.attempt = moved.attempt "
        "returning a.job_id, moved.state"
    )


# ends a job as cancelled, but only for the attempt that holds its live lease
_FINISH_CANCELLED = (
    "with now as materialized (select clock_timestamp() as at), "
    "done as ("
    "update fleet_jobs as j set state = 'cancelled', lease_expires_at = null, "
    "finished_at = now.at, updated_at = now.at "
    "from now where j.id = :job and j.attempt = :attempt and j.state in ('claimed', 'running') "
    "and j.lease_expires_at > now.at and j.cancel_requested_at is not null "
    "returning j.id, j.attempt) "
    "update fleet_attempts as a set ended_at = now.at, ended_by = 'cancelled', error = 'cancelled' "
    "from done, now where a.job_id = done.id and a.attempt = done.attempt returning a.job_id"
)


class Beat(BaseModel):
    """A heartbeat's answer: the lease now runs until here, and whether to stop."""

    lease_expires_at: datetime
    cancel_requested: bool


class IdempotencyConflictError(Exception):
    """An idempotency key was reused for a different batch."""


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json(value: Any) -> dict[str, Any]:
    # asyncpg hands jsonb back as text unless a codec is registered
    loaded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(loaded, dict):
        raise TypeError(f"expected a json object, got {type(loaded).__name__}")
    return loaded


def request_digest(
    label: str, jobs: Sequence[NewJob], max_attempts: int, policy: ExecutionPolicy
) -> str:
    return _digest(
        {
            "label": label,
            "max_attempts": max_attempts,
            "policy": policy.model_dump(mode="json"),
            "jobs": [job.model_dump(mode="json") for job in jobs],
        }
    )


def _status(row: Row[Any]) -> JobStatus:
    return JobStatus.model_validate({**row._asdict(), "policy": _json(row.policy)})


async def submit_batch(
    engine: AsyncEngine,
    *,
    label: str,
    jobs: Sequence[NewJob],
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    policy: ExecutionPolicy = DEFAULT_POLICY,
) -> Submission:
    """Enqueue a batch in one transaction; a repeated key returns the original batch."""
    if not jobs:
        raise ValueError("a batch needs at least one job")
    if max_attempts < 1:
        raise ValueError("every job needs at least one attempt")
    digest = request_digest(label, jobs, max_attempts, policy)
    async with engine.begin() as connection:
        # a concurrent insert of the same key waits here and then conflicts, so one batch wins
        batch_id = await connection.scalar(
            text(
                "insert into fleet_batches (idempotency_key, request_digest, label) "
                "values (:key, :digest, :label) "
                "on conflict (idempotency_key) do nothing returning id"
            ),
            {"key": idempotency_key, "digest": digest, "label": label},
        )
        if batch_id is None:
            existing = (
                await connection.execute(
                    text(
                        "select id, request_digest from fleet_batches where idempotency_key = :key"
                    ),
                    {"key": idempotency_key},
                )
            ).one()
            if existing.request_digest != digest:
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} was already used for a different batch"
                )
            existing_ids = await connection.scalars(
                text("select id from fleet_jobs where batch_id = :batch order by position"),
                {"batch": existing.id},
            )
            return Submission(batch_id=existing.id, job_ids=list(existing_ids), created=False)
        payloads = [json.dumps(job.payload, sort_keys=True) for job in jobs]
        rows = await connection.execute(
            text(
                "insert into fleet_jobs "
                "(batch_id, position, name, payload, payload_digest, max_attempts, policy) "
                "select :batch, item.position, item.name, item.payload::jsonb, item.digest, "
                ":max_attempts, CAST(:policy AS jsonb) "
                "from unnest(CAST(:positions AS integer[]), CAST(:names AS text[]), "
                "CAST(:payloads AS text[]), CAST(:digests AS text[])) "
                "as item(position, name, payload, digest) "
                "returning id, position"
            ),
            {
                "batch": batch_id,
                "max_attempts": max_attempts,
                "policy": policy.model_dump_json(),
                "positions": list(range(len(jobs))),
                "names": [job.name for job in jobs],
                "payloads": payloads,
                "digests": [_digest(job.payload) for job in jobs],
            },
        )
        ids = [row.id for row in sorted(rows, key=lambda row: row.position)]
    return Submission(batch_id=batch_id, job_ids=ids, created=True)


async def claim(engine: AsyncEngine, *, worker_id: str, lease_seconds: float) -> ClaimedJob | None:
    """Lease the oldest queued job; SKIP LOCKED lets concurrent claimers pass each other's rows."""
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    "update fleet_jobs set state = 'claimed', attempt = attempt + 1, "
                    "worker_id = :worker, claimed_at = clock_timestamp(), started_at = null, "
                    "lease_expires_at = clock_timestamp() "
                    "+ make_interval(secs => CAST(:lease AS double precision)), "
                    "updated_at = clock_timestamp() "
                    "where id = (select id from fleet_jobs where state = 'queued' "
                    "and available_at <= clock_timestamp() "
                    "order by id limit 1 for update skip locked) "
                    "returning id, attempt, name, payload, claimed_at, lease_expires_at, policy"
                ),
                {"worker": worker_id, "lease": lease_seconds},
            )
        ).first()
        if row is None:
            return None
        await connection.execute(
            text(
                "insert into fleet_attempts "
                "(job_id, attempt, worker_id, claimed_at, lease_expires_at) "
                "values (:job, :attempt, :worker, :claimed_at, :lease_expires_at)"
            ),
            {
                "job": row.id,
                "attempt": row.attempt,
                "worker": worker_id,
                "claimed_at": row.claimed_at,
                "lease_expires_at": row.lease_expires_at,
            },
        )
    return ClaimedJob(
        id=row.id,
        attempt=row.attempt,
        name=row.name,
        payload=_json(row.payload),
        lease_expires_at=row.lease_expires_at,
        policy=ExecutionPolicy.model_validate(_json(row.policy)),
    )


async def start(engine: AsyncEngine, *, job_id: int, attempt: int) -> bool:
    """Mark a claimed job running; False unless this attempt still holds a live lease."""
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "update fleet_jobs set state = 'running', started_at = clock_timestamp(), "
                "updated_at = clock_timestamp() "
                "where id = :job and attempt = :attempt and state = 'claimed' "
                "and lease_expires_at > clock_timestamp()"
            ),
            {"job": job_id, "attempt": attempt},
        )
        return result.rowcount == 1


async def heartbeat(
    engine: AsyncEngine, *, job_id: int, attempt: int, lease_seconds: float
) -> Beat | None:
    """Extend a live lease to lease_seconds from now; None once this attempt has lost the job."""
    async with engine.begin() as connection:
        # the attempt log keeps the extended lease, so each result can be checked against it
        row = (
            await connection.execute(
                text(
                    "with beat as ("
                    "update fleet_jobs set lease_expires_at = clock_timestamp() "
                    "+ make_interval(secs => CAST(:lease AS double precision)), "
                    "updated_at = clock_timestamp() "
                    "where id = :job and attempt = :attempt and state in ('claimed', 'running') "
                    "and lease_expires_at > clock_timestamp() "
                    "returning id, attempt, lease_expires_at, cancel_requested_at) "
                    "update fleet_attempts as a set lease_expires_at = beat.lease_expires_at "
                    "from beat where a.job_id = beat.id and a.attempt = beat.attempt "
                    "returning a.lease_expires_at, beat.cancel_requested_at is not null "
                    "as cancel_requested"
                ),
                {"job": job_id, "attempt": attempt, "lease": lease_seconds},
            )
        ).first()
    return None if row is None else Beat.model_validate(row._asdict())


async def cancel(engine: AsyncEngine, job_id: int) -> JobState | None:
    """Cancel now if not running, else ask its worker to stop; the state after, None if unknown."""
    async with engine.begin() as connection:
        state = await connection.scalar(
            text(
                "update fleet_jobs set "
                "state = case when state = 'queued' then 'cancelled' else state end, "
                "finished_at = case when state = 'queued' then clock_timestamp() "
                "else finished_at end, "
                "cancel_requested_at = coalesce(cancel_requested_at, clock_timestamp()), "
                "updated_at = clock_timestamp() "
                "where id = :job and state in ('queued', 'claimed', 'running') returning state"
            ),
            {"job": job_id},
        )
        if state is None:
            state = await connection.scalar(
                text("select state from fleet_jobs where id = :job"), {"job": job_id}
            )
    return None if state is None else cast(JobState, state)


async def finish_cancelled(engine: AsyncEngine, *, job_id: int, attempt: int) -> bool:
    """End a job whose cancel was requested; False unless this attempt's lease is live."""
    async with engine.begin() as connection:
        ended = await connection.scalar(
            text(_FINISH_CANCELLED), {"job": job_id, "attempt": attempt}
        )
    return ended is not None


async def publish(
    engine: AsyncEngine,
    *,
    job_id: int,
    attempt: int,
    worker_id: str,
    outcome: Outcome,
    body: dict[str, Any],
) -> bool:
    """Publish a result and finish the job atomically; False unless this attempt's lease is live."""
    async with engine.begin() as connection:
        # the row lock makes the ownership check and the write a single step
        held = (
            await connection.execute(
                text(
                    "select lease_expires_at, cancel_requested_at from fleet_jobs "
                    "where id = :job and attempt = :attempt "
                    "and state in ('claimed', 'running') for update"
                ),
                {"job": job_id, "attempt": attempt},
            )
        ).first()
        if held is None:
            return False
        if held.cancel_requested_at is not None:
            # a cancel requested before the result arrived wins: the job ends cancelled instead
            await connection.execute(text(_FINISH_CANCELLED), {"job": job_id, "attempt": attempt})
            return False
        lease = held.lease_expires_at
        # read under the lock, one clock reading is both the lease check and the publish time
        stored = (
            await connection.execute(
                text(
                    "insert into fleet_results "
                    "(job_id, attempt, worker_id, published_at, outcome, body) "
                    "select :job, :attempt, :worker, now.at, :outcome, CAST(:body AS jsonb) "
                    "from (select clock_timestamp() as at) as now "
                    "where now.at < CAST(:lease AS timestamptz) "
                    "returning id, published_at"
                ),
                {
                    "job": job_id,
                    "attempt": attempt,
                    "worker": worker_id,
                    "outcome": outcome,
                    "body": json.dumps(body, sort_keys=True),
                    "lease": lease,
                },
            )
        ).first()
        if stored is None:
            return False
        await connection.execute(
            text(
                "update fleet_jobs set state = :outcome, result_id = :result, "
                "lease_expires_at = null, finished_at = :at, updated_at = :at where id = :job"
            ),
            {"job": job_id, "outcome": outcome, "result": stored.id, "at": stored.published_at},
        )
        await connection.execute(
            text(
                "update fleet_attempts set ended_at = :at, ended_by = 'published' "
                "where job_id = :job and attempt = :attempt"
            ),
            {"job": job_id, "attempt": attempt, "at": stored.published_at},
        )
    return True


async def release(
    engine: AsyncEngine,
    *,
    job_id: int,
    attempt: int,
    error: str,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> JobState | None:
    """Give a job back after an infrastructure failure; its new state, or None if it wasn't ours."""
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    _end_attempts(
                        "select id from fleet_jobs where id = :job and attempt = :attempt "
                        "and state in ('claimed', 'running') "
                        "and lease_expires_at > (select at from now) for update",
                        "released",
                    )
                ),
                {
                    "job": job_id,
                    "attempt": attempt,
                    "error": error,
                    "backoff": retry.backoff_seconds,
                    "cap": retry.backoff_cap_seconds,
                },
            )
        ).first()
    return None if row is None else cast(JobState, row.state)


async def reap(engine: AsyncEngine, *, retry: RetryPolicy = DEFAULT_RETRY) -> list[int]:
    """End every attempt whose lease ran out, returning its job to the queue or dead letters."""
    async with engine.begin() as connection:
        # skip locked lets concurrent reapers split the lapsed jobs instead of queueing on them
        rows = await connection.execute(
            text(
                _end_attempts(
                    "select id from fleet_jobs where state in ('claimed', 'running') "
                    "and lease_expires_at <= (select at from now) for update skip locked",
                    "lease_expired",
                )
            ),
            {
                "error": LEASE_EXPIRED,
                "backoff": retry.backoff_seconds,
                "cap": retry.backoff_cap_seconds,
            },
        )
        return sorted(row.job_id for row in rows)


async def unfinished_jobs(engine: AsyncEngine) -> int:
    """How many jobs anywhere still wait to run or finish."""
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("select count(*) from fleet_jobs where state = any(:states)"),
            {"states": sorted(UNFINISHED_STATES)},
        )
        return int(count or 0)


async def batch_status(engine: AsyncEngine, batch_id: int) -> BatchStatus | None:
    async with engine.connect() as connection:
        label = await connection.scalar(
            text("select label from fleet_batches where id = :batch"), {"batch": batch_id}
        )
        if label is None:
            return None
        rows = await connection.execute(
            text(
                "select state, count(*) as jobs from fleet_jobs where batch_id = :batch "
                "group by state"
            ),
            {"batch": batch_id},
        )
        counts = {row.state: int(row.jobs) for row in rows}
    return BatchStatus(
        batch_id=batch_id,
        label=label,
        total=sum(counts.values()),
        counts=counts,
        done=not any(state in UNFINISHED_STATES for state in counts),
    )


async def job_status(engine: AsyncEngine, job_id: int) -> JobStatus | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(f"select {_JOB_COLUMNS} from fleet_jobs where id = :job"), {"job": job_id}
            )
        ).first()
    return None if row is None else _status(row)


async def batch_jobs(engine: AsyncEngine, batch_id: int) -> list[JobStatus]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                f"select {_JOB_COLUMNS} from fleet_jobs where batch_id = :batch order by position"
            ),
            {"batch": batch_id},
        )
        return [_status(row) for row in rows]


async def job_result(engine: AsyncEngine, job_id: int) -> PublishedResult | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "select job_id, attempt, worker_id, published_at, outcome, body "
                    "from fleet_results where job_id = :job"
                ),
                {"job": job_id},
            )
        ).first()
    if row is None:
        return None
    return PublishedResult.model_validate({**row._asdict(), "body": _json(row.body)})


async def server_version(engine: AsyncEngine) -> str:
    """The server's name and version, e.g. "PostgreSQL 16.13", for run records."""
    async with engine.connect() as connection:
        full = await connection.scalar(text("select version()"))
    return " ".join(str(full).split()[:2])
