"""Runners, process helpers and restart checks shared by the fleet tests."""

import asyncio
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import RetryPolicy, Submission
from fleet.worker import RunnerOutcome

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# retries at once, so tests that take jobs back need not wait out a real backoff
NO_BACKOFF = RetryPolicy(backoff_seconds=0.0)


async def sleep_runner(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    """A stand-in job: sleeps for payload["sleep_ms"] and reports what it did."""
    sleep_ms = int(payload.get("sleep_ms", 0))
    await asyncio.sleep(sleep_ms / 1000)
    return RunnerOutcome(outcome="succeeded", body={"name": name, "slept_ms": sleep_ms})


def start_worker(
    url: str,
    *,
    runner: str = "tests.fleet_helpers:sleep_runner",
    worker_id: str = "w0",
    lease_seconds: float = 60.0,
    retry_backoff_seconds: float | None = None,
    exit_when_idle: bool = False,
) -> "subprocess.Popen[str]":
    """Start `python -m fleet.worker` as its own process, the way a deployment would."""
    command = [
        sys.executable,
        "-m",
        "fleet.worker",
        "--runner",
        runner,
        "--worker-id",
        worker_id,
        "--lease-seconds",
        str(lease_seconds),
        "--database-url",
        url,
    ]
    if retry_backoff_seconds is not None:
        command += ["--retry-backoff-seconds", str(retry_backoff_seconds)]
    if exit_when_idle:
        command.append("--exit-when-idle")
    return subprocess.Popen(
        command,
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def rows(engine: AsyncEngine, sql: str, batch_id: int) -> list[Row[Any]]:
    async with engine.connect() as connection:
        return list(await connection.execute(text(sql), {"batch": batch_id}))


async def wait_for_published(engine: AsyncEngine, batch_id: int, target: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        published = await rows(
            engine,
            "select count(*) from fleet_results r join fleet_jobs j on j.id = r.job_id "
            "where j.batch_id = :batch",
            batch_id,
        )
        if published[0][0] >= target:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"the first worker never published {target} results")


async def kill_mid_batch_and_restart(
    engine: AsyncEngine,
    url: str,
    submission: Submission,
    *,
    runner: str,
    rng: random.Random,
    lease_seconds: float,
    max_published_before_kill: int,
    kill_within_seconds: float,
) -> set[int]:
    """SIGKILL a worker at a seeded point mid-batch, then run a fresh one to the end.

    Returns the jobs the killed worker was holding, which the fresh one must re-run.
    """
    first = start_worker(
        url,
        runner=runner,
        worker_id="first",
        lease_seconds=lease_seconds,
        retry_backoff_seconds=0.1,
    )
    # a seeded kill point: after some jobs are published, partway into whatever runs next
    target = rng.randint(0, max_published_before_kill)
    await wait_for_published(engine, submission.batch_id, target)
    await asyncio.sleep(rng.uniform(0.0, kill_within_seconds))
    first.kill()
    await asyncio.to_thread(first.wait)
    held = await rows(
        engine,
        "select id from fleet_jobs where batch_id = :batch and state in ('claimed', 'running')",
        submission.batch_id,
    )
    in_flight = {row[0] for row in held}
    print(f"killed after {target}+ published, {len(in_flight)} job(s) in flight")
    second = start_worker(
        url,
        runner=runner,
        worker_id="second",
        lease_seconds=lease_seconds,
        retry_backoff_seconds=0.1,
        exit_when_idle=True,
    )
    output, _ = await asyncio.to_thread(second.communicate, timeout=300)
    assert second.returncode == 0, output
    return in_flight


async def assert_every_job_accounted_for_once(
    engine: AsyncEngine, submission: Submission, in_flight: set[int]
) -> None:
    jobs = await rows(
        engine,
        "select id, state, attempt, result_id from fleet_jobs where batch_id = :batch order by id",
        submission.batch_id,
    )
    assert [row[0] for row in jobs] == submission.job_ids
    # no lost job: every one reached a final state
    assert {row[1] for row in jobs} <= {"succeeded", "failed", "escalated"}
    results = await rows(
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
    attempts = await rows(
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
