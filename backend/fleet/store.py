"""The fleet's job store: every state change is one transaction on Postgres's clock."""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import (
    UNFINISHED_STATES,
    BatchStatus,
    ClaimedJob,
    JobStatus,
    NewJob,
    Outcome,
    PublishedResult,
    Submission,
)

_JOB_COLUMNS = (
    "id, batch_id, position, name, state, attempt, worker_id, lease_expires_at, result_id, "
    "submitted_at, claimed_at, started_at, finished_at"
)


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


def request_digest(label: str, jobs: Sequence[NewJob]) -> str:
    return _digest({"label": label, "jobs": [job.model_dump(mode="json") for job in jobs]})


async def submit_batch(
    engine: AsyncEngine,
    *,
    label: str,
    jobs: Sequence[NewJob],
    idempotency_key: str | None = None,
) -> Submission:
    """Enqueue a batch in one transaction; a repeated key returns the original batch."""
    if not jobs:
        raise ValueError("a batch needs at least one job")
    digest = request_digest(label, jobs)
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
                "insert into fleet_jobs (batch_id, position, name, payload, payload_digest) "
                "select :batch, item.position, item.name, item.payload::jsonb, item.digest "
                "from unnest(CAST(:positions AS integer[]), CAST(:names AS text[]), "
                "CAST(:payloads AS text[]), CAST(:digests AS text[])) "
                "as item(position, name, payload, digest) "
                "returning id, position"
            ),
            {
                "batch": batch_id,
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
                    "order by id limit 1 for update skip locked) "
                    "returning id, attempt, name, payload, claimed_at, lease_expires_at"
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
    )


async def start(engine: AsyncEngine, *, job_id: int, attempt: int) -> bool:
    """Mark a claimed job running; False when this attempt no longer owns it."""
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "update fleet_jobs set state = 'running', started_at = clock_timestamp(), "
                "updated_at = clock_timestamp() "
                "where id = :job and attempt = :attempt and state = 'claimed'"
            ),
            {"job": job_id, "attempt": attempt},
        )
        return result.rowcount == 1


async def publish(
    engine: AsyncEngine,
    *,
    job_id: int,
    attempt: int,
    worker_id: str,
    outcome: Outcome,
    body: dict[str, Any],
) -> bool:
    """Store a job's result and finish it atomically; False when this attempt no longer owns it."""
    async with engine.begin() as connection:
        # the row lock makes the ownership check and the write a single step
        owner = (
            await connection.execute(
                text(
                    "select 1 from fleet_jobs where id = :job and attempt = :attempt "
                    "and state in ('claimed', 'running') for update"
                ),
                {"job": job_id, "attempt": attempt},
            )
        ).first()
        if owner is None:
            return False
        result_id = await connection.scalar(
            text(
                "insert into fleet_results "
                "(job_id, attempt, worker_id, published_at, outcome, body) "
                "values (:job, :attempt, :worker, clock_timestamp(), :outcome, "
                "CAST(:body AS jsonb)) returning id"
            ),
            {
                "job": job_id,
                "attempt": attempt,
                "worker": worker_id,
                "outcome": outcome,
                "body": json.dumps(body, sort_keys=True),
            },
        )
        await connection.execute(
            text(
                "update fleet_jobs set state = :outcome, result_id = :result, "
                "lease_expires_at = null, finished_at = clock_timestamp(), "
                "updated_at = clock_timestamp() where id = :job"
            ),
            {"job": job_id, "outcome": outcome, "result": result_id},
        )
        await connection.execute(
            text(
                "update fleet_attempts set ended_at = clock_timestamp(), ended_by = 'published' "
                "where job_id = :job and attempt = :attempt"
            ),
            {"job": job_id, "attempt": attempt},
        )
    return True


async def reap(engine: AsyncEngine) -> list[int]:
    """Return jobs whose lease ran out to the queue, closing the attempt that let it lapse."""
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                "with expired as ("
                "update fleet_jobs set state = 'queued', worker_id = null, "
                "lease_expires_at = null, claimed_at = null, started_at = null, "
                "updated_at = clock_timestamp() "
                "where state in ('claimed', 'running') and lease_expires_at < clock_timestamp() "
                "returning id, attempt) "
                "update fleet_attempts as a "
                "set ended_at = clock_timestamp(), ended_by = 'lease_expired' "
                "from expired where a.job_id = expired.id and a.attempt = expired.attempt "
                "returning a.job_id"
            )
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
    return None if row is None else JobStatus.model_validate(row._asdict())


async def batch_jobs(engine: AsyncEngine, batch_id: int) -> list[JobStatus]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                f"select {_JOB_COLUMNS} from fleet_jobs where batch_id = :batch order by position"
            ),
            {"batch": batch_id},
        )
        return [JobStatus.model_validate(row._asdict()) for row in rows]


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
