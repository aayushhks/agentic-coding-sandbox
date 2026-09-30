"""Kill the worker mid-batch with SIGKILL, restart it, and account for every job exactly once."""

import asyncio
import random
import time
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob, Submission
from fleet.store import submit_batch
from tests.fleet_helpers import start_worker

# short, so a dead worker's job comes back quickly; still longer than any job here
LEASE_SECONDS = 1.0


async def _rows(engine: AsyncEngine, sql: str, batch_id: int) -> list[Row[Any]]:
    async with engine.connect() as connection:
        return list(await connection.execute(text(sql), {"batch": batch_id}))


async def _wait_for_published(engine: AsyncEngine, batch_id: int, target: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        rows = await _rows(
            engine,
            "select count(*) from fleet_results r join fleet_jobs j on j.id = r.job_id "
            "where j.batch_id = :batch",
            batch_id,
        )
        if rows[0][0] >= target:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"the first worker never published {target} results")


async def assert_every_job_accounted_for_once(
    engine: AsyncEngine, submission: Submission, in_flight: set[int]
) -> None:
    jobs = await _rows(
        engine,
        "select id, state, attempt, result_id from fleet_jobs where batch_id = :batch order by id",
        submission.batch_id,
    )
    assert [row[0] for row in jobs] == submission.job_ids
    # no lost job: every one reached a final state
    assert {row[1] for row in jobs} == {"succeeded"}
    results = await _rows(
        engine,
        "select r.id, r.job_id, r.attempt, r.worker_id from fleet_results r "
        "join fleet_jobs j on j.id = r.job_id where j.batch_id = :batch",
        submission.batch_id,
    )
    # no duplicate: exactly one published result per job, and it is the one the job points at
    assert sorted(row[1] for row in results) == submission.job_ids
    by_job = {row[1]: row for row in results}
    for job_id, _state, attempt, result_id in jobs:
        assert by_job[job_id][0] == result_id
        assert by_job[job_id][2] == attempt
    attempts = await _rows(
        engine,
        "select a.job_id, a.attempt, a.ended_by from fleet_attempts a "
        "join fleet_jobs j on j.id = a.job_id where j.batch_id = :batch",
        submission.batch_id,
    )
    for job_id, _state, attempt, _result in jobs:
        log = sorted((row[1], row[2]) for row in attempts if row[0] == job_id)
        # the attempt log matches the job's attempt count, every attempt closed, one published
        assert [number for number, _ in log] == list(range(1, attempt + 1))
        assert [ended for _, ended in log] == ["lease_expired"] * (len(log) - 1) + ["published"]
    for job_id in in_flight:
        assert by_job[job_id][3] == "second", f"job {job_id} was re-run by the new worker"


@pytest.mark.parametrize("seed", range(10))
async def test_a_killed_worker_loses_and_duplicates_nothing(
    fleet_engine: AsyncEngine, fleet_database_url: str, seed: int
) -> None:
    rng = random.Random(seed)
    jobs = [NewJob(name=f"job-{n}", payload={"sleep_ms": rng.randint(10, 60)}) for n in range(50)]
    submission = await submit_batch(
        fleet_engine, label=f"restart-{seed}", jobs=jobs, idempotency_key=f"restart-{seed}"
    )
    first = start_worker(fleet_database_url, worker_id="first", lease_seconds=LEASE_SECONDS)
    # a seeded kill point: after some jobs are published, partway into whatever runs next
    target = rng.randint(0, 45)
    await _wait_for_published(fleet_engine, submission.batch_id, target)
    await asyncio.sleep(rng.uniform(0.0, 0.06))
    first.kill()
    await asyncio.to_thread(first.wait)
    held = await _rows(
        fleet_engine,
        "select id from fleet_jobs where batch_id = :batch and state in ('claimed', 'running')",
        submission.batch_id,
    )
    in_flight = {row[0] for row in held}
    print(f"seed {seed}: killed after {target}+ published, {len(in_flight)} job(s) in flight")

    second = start_worker(
        fleet_database_url, worker_id="second", lease_seconds=LEASE_SECONDS, exit_when_idle=True
    )
    output, _ = await asyncio.to_thread(second.communicate, timeout=120)
    assert second.returncode == 0, output
    await assert_every_job_accounted_for_once(fleet_engine, submission, in_flight)
