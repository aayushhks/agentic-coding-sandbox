"""A worker process paused past its lease wakes up to find its job taken, and cannot write to it."""

import asyncio
import signal
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob
from fleet.store import job_result, job_status, submit_batch
from tests.fleet_helpers import assert_invariants_hold, start_worker


async def _wait_until_running(engine: AsyncEngine, job_id: int) -> None:
    deadline = time.monotonic() + 30
    while (status := await job_status(engine, job_id)) is None or status.state != "running":
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} never started")
        await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    ("sleep_ms", "heartbeat_seconds", "caught"),
    [
        # a job longer than a heartbeat: back from the pause, the next heartbeat finds the lease
        # gone and stops the run
        (1500, None, "0 published, 0 rejected, 1 lost"),
        # a job shorter than a heartbeat: it finishes first, and its late result is refused
        (300, 0.9, "0 published, 1 rejected, 0 lost"),
    ],
    ids=["heartbeat-finds-the-lease-gone", "late-result-refused"],
)
async def test_a_worker_paused_past_its_lease_cannot_write_to_the_job(
    fleet_engine: AsyncEngine,
    fleet_database_url: str,
    sleep_ms: int,
    heartbeat_seconds: float | None,
    caught: str,
) -> None:
    submission = await submit_batch(
        fleet_engine, label="pause", jobs=[NewJob(name="j", payload={"sleep_ms": sleep_ms})]
    )
    job_id = submission.job_ids[0]
    paused = start_worker(
        fleet_database_url,
        worker_id="paused",
        lease_seconds=1.0,
        heartbeat_seconds=heartbeat_seconds,
        retry_backoff_seconds=0.0,
    )
    await _wait_until_running(fleet_engine, job_id)
    paused.send_signal(signal.SIGSTOP)
    try:
        # its lease lapses while it is stopped, so a second worker takes the job back and runs it
        second = start_worker(
            fleet_database_url,
            worker_id="second",
            lease_seconds=1.0,
            retry_backoff_seconds=0.0,
            exit_when_idle=True,
        )
        output, _ = await asyncio.to_thread(second.communicate, timeout=60)
        assert second.returncode == 0, output
        assert "second: 1 published" in output
    finally:
        paused.send_signal(signal.SIGCONT)
    # a stop request lets the resumed worker finish what it was doing, then report
    paused.send_signal(signal.SIGTERM)
    output, _ = await asyncio.to_thread(paused.communicate, timeout=60)
    assert f"paused: {caught}, 0 released" in output, output
    result = await job_result(fleet_engine, job_id)
    assert result is not None and (result.attempt, result.worker_id) == (2, "second")
    await assert_invariants_hold(fleet_engine, submission.job_ids)
