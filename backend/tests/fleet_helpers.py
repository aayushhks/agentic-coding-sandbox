"""Runners, process helpers and restart checks shared by the fleet tests."""

import asyncio
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.invariants import check, snapshot
from fleet.models import RetryPolicy, Submission
from fleet.store import job_result
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
    heartbeat_seconds: float | None = None,
    reap_every_seconds: float | None = None,
    retry_backoff_seconds: float | None = None,
    exit_when_idle: bool = False,
    output: IO[str] | int = subprocess.PIPE,
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
    for flag, value in (
        ("--heartbeat-seconds", heartbeat_seconds),
        ("--reap-every-seconds", reap_every_seconds),
        ("--retry-backoff-seconds", retry_backoff_seconds),
    ):
        if value is not None:
            command += [flag, str(value)]
    if exit_when_idle:
        command.append("--exit-when-idle")
    return subprocess.Popen(
        command,
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
        stdout=output,
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


async def assert_invariants_hold(engine: AsyncEngine, job_ids: list[int]) -> None:
    violations = check(await snapshot(engine, job_ids), job_ids)
    assert not violations, "\n".join(str(violation) for violation in violations)


async def assert_every_job_accounted_for_once(
    engine: AsyncEngine, submission: Submission, in_flight: set[int]
) -> None:
    # nothing lost, nothing duplicated, no stale write, and an attempt log that adds up
    await assert_invariants_hold(engine, submission.job_ids)
    for job_id in in_flight:
        result = await job_result(engine, job_id)
        assert result is not None and result.worker_id == "second", (
            f"job {job_id} was re-run by the new worker"
        )
