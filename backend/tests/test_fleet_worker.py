import asyncio
import contextlib
import time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob
from fleet.store import batch_jobs, cancel, claim, job_result, job_status, reap, submit_batch
from fleet.worker import RunnerOutcome, Worker, load_runner
from tests.fleet_helpers import NO_BACKOFF, sleep_runner, start_worker


async def _double(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    if payload["n"] == 3:
        raise RuntimeError("boom")
    return RunnerOutcome(outcome="succeeded", body={"name": name, "doubled": payload["n"] * 2})


def _worker(engine: AsyncEngine, runner: Any = _double, **options: Any) -> Worker:
    return Worker(engine, runner, worker_id="w", lease_seconds=60, **options)


async def test_the_worker_runs_every_job_and_publishes_its_result(
    fleet_engine: AsyncEngine,
) -> None:
    jobs = [NewJob(name=f"j{n}", payload={"n": n}) for n in (1, 2, 4)]
    submission = await submit_batch(fleet_engine, label="b", jobs=jobs)
    worker = _worker(fleet_engine)
    assert await worker.run(exit_when_idle=True) == 3
    results = [await job_result(fleet_engine, job_id) for job_id in submission.job_ids]
    assert [result.body["doubled"] for result in results if result is not None] == [2, 4, 8]


async def _endings(engine: AsyncEngine, job_id: int) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            text("select ended_by from fleet_attempts where job_id = :job order by attempt"),
            {"job": job_id},
        )
        return list(rows)


async def test_a_runner_that_always_crashes_is_retried_then_dead_lettered(
    fleet_engine: AsyncEngine,
) -> None:
    jobs = [NewJob(name=f"j{n}", payload={"n": n}) for n in (3, 4)]
    submission = await submit_batch(fleet_engine, label="b", jobs=jobs)
    worker = _worker(fleet_engine, retry=NO_BACKOFF)
    await asyncio.wait_for(worker.run(exit_when_idle=True), timeout=10)
    assert (worker.published, worker.released) == (1, 3)
    crashed, fine = await batch_jobs(fleet_engine, submission.batch_id)
    assert (crashed.state, crashed.attempt, crashed.last_error) == (
        "dead_lettered",
        3,
        "RuntimeError: boom",
    )
    assert await job_result(fleet_engine, crashed.id) is None
    assert await _endings(fleet_engine, crashed.id) == ["released"] * 3
    assert (fine.state, fine.attempt) == ("succeeded", 1)


async def test_a_passing_infrastructure_failure_is_retried_until_the_job_runs(
    fleet_engine: AsyncEngine,
) -> None:
    calls = 0

    async def flaky(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("model api unreachable")
        return RunnerOutcome(outcome="succeeded", body={})

    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])
    await _worker(fleet_engine, flaky, retry=NO_BACKOFF).run(exit_when_idle=True)
    job = await job_status(fleet_engine, submission.job_ids[0])
    assert job is not None and (job.state, job.attempt) == ("succeeded", 2)
    assert job.last_error == "ConnectionError: model api unreachable"
    assert await _endings(fleet_engine, job.id) == ["released", "published"]


async def test_a_task_failure_is_final_on_the_first_attempt(fleet_engine: AsyncEngine) -> None:
    async def gives_up(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        return RunnerOutcome(outcome="failed", body={"reason": "could not solve it"})

    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])
    worker = _worker(fleet_engine, gives_up, retry=NO_BACKOFF)
    await worker.run(exit_when_idle=True)
    assert (worker.published, worker.released) == (1, 0)
    job = await job_status(fleet_engine, submission.job_ids[0])
    assert job is not None and (job.state, job.attempt, job.last_error) == ("failed", 1, None)
    assert await _endings(fleet_engine, job.id) == ["published"]


async def test_exiting_when_idle_waits_for_an_abandoned_job(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(
        fleet_engine, label="b", jobs=[NewJob(name="j", payload={"n": 1})]
    )
    # a worker that claimed the job and died: its lease simply runs out
    abandoned = await claim(fleet_engine, worker_id="dead", lease_seconds=0.2)
    assert abandoned is not None
    worker = _worker(fleet_engine, reap_every_seconds=0.05, min_poll_seconds=0.02, retry=NO_BACKOFF)
    assert await asyncio.wait_for(worker.run(exit_when_idle=True), timeout=10) == 1
    job = (await batch_jobs(fleet_engine, submission.batch_id))[0]
    assert (job.state, job.attempt, job.worker_id) == ("succeeded", 2, "w")


async def test_a_stop_request_lets_the_current_job_finish(fleet_engine: AsyncEngine) -> None:
    stop = asyncio.Event()

    async def stop_after_first(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        stop.set()
        return RunnerOutcome(outcome="succeeded", body={})

    jobs = [NewJob(name=f"j{n}", payload={"n": n}) for n in range(3)]
    submission = await submit_batch(fleet_engine, label="b", jobs=jobs)
    assert await _worker(fleet_engine, stop_after_first).run(stop=stop) == 1
    states = [job.state for job in await batch_jobs(fleet_engine, submission.batch_id)]
    assert states == ["succeeded", "queued", "queued"]


async def test_an_idle_worker_backs_off_its_polling(fleet_engine: AsyncEngine) -> None:
    stop = asyncio.Event()
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 6:
            stop.set()

    await _worker(fleet_engine, sleep=record).run(stop=stop)
    assert delays == [0.05, 0.1, 0.2, 0.4, 0.5, 0.5]


async def _sleeps(seconds: float) -> RunnerOutcome:
    await asyncio.sleep(seconds)
    return RunnerOutcome(outcome="succeeded", body={})


async def test_heartbeats_let_a_job_run_well_past_its_lease(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])

    async def reap_constantly() -> None:
        while True:
            await reap(fleet_engine)
            await asyncio.sleep(0.02)

    reaper = asyncio.create_task(reap_constantly())
    worker = Worker(fleet_engine, lambda _n, _p: _sleeps(0.9), worker_id="w", lease_seconds=0.3)
    try:
        assert await worker.step()
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
    # three leases long, yet a reaper running all along never took it: the heartbeats kept it
    assert (worker.published, worker.lost) == (1, 0)
    job = await job_status(fleet_engine, submission.job_ids[0])
    assert job is not None and (job.state, job.attempt) == ("succeeded", 1)
    async with fleet_engine.connect() as connection:
        attempt = (
            await connection.execute(
                text("select claimed_at, lease_expires_at from fleet_attempts")
            )
        ).one()
    assert (attempt.lease_expires_at - attempt.claimed_at).total_seconds() > 0.9


async def test_a_worker_that_loses_its_lease_stops_the_run(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])
    stopped = asyncio.Event()

    async def stalls(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        await asyncio.sleep(0.05)
        # blocking the event loop starves the heartbeats until the lease has run out
        time.sleep(0.5)  # noqa: ASYNC251
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return RunnerOutcome(outcome="succeeded", body={})

    worker = Worker(fleet_engine, stalls, worker_id="w", lease_seconds=0.2)
    assert await asyncio.wait_for(worker.step(), timeout=5)
    assert (worker.published, worker.rejected, worker.lost) == (0, 0, 1)
    assert stopped.is_set()
    assert await job_result(fleet_engine, submission.job_ids[0]) is None


async def test_a_result_finished_after_the_lease_ran_out_is_refused(
    fleet_engine: AsyncEngine,
) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])

    async def blocks(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        # no heartbeat can run while the loop is blocked, so the lease lapses mid-job
        time.sleep(0.5)  # noqa: ASYNC251
        return RunnerOutcome(outcome="succeeded", body={"late": True})

    stalled = Worker(fleet_engine, blocks, worker_id="stalled", lease_seconds=0.2)
    assert await stalled.step()
    assert (stalled.published, stalled.rejected, stalled.lost) == (0, 1, 0)
    # nobody had taken the job back yet; the lapsed lease alone refused the result
    job = await job_status(fleet_engine, submission.job_ids[0])
    assert job is not None and (job.state, job.result_id) == ("running", None)
    rescuer = _worker(
        fleet_engine, lambda _n, _p: _sleeps(0), min_poll_seconds=0.01, retry=NO_BACKOFF
    )
    await asyncio.wait_for(rescuer.run(exit_when_idle=True), timeout=10)
    result = await job_result(fleet_engine, submission.job_ids[0])
    assert result is not None and (result.attempt, result.worker_id, result.body) == (2, "w", {})


def test_a_heartbeat_has_to_come_before_the_lease_runs_out(fleet_engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="sooner than the lease"):
        Worker(fleet_engine, sleep_runner, worker_id="w", lease_seconds=1, heartbeat_seconds=1)


def test_runners_are_named_as_module_and_function() -> None:
    assert load_runner("tests.fleet_helpers:sleep_runner") is sleep_runner
    with pytest.raises(ValueError, match="module:function"):
        load_runner("tests.fleet_helpers")


async def test_the_worker_command_drains_a_batch(
    fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    jobs = [NewJob(name=f"j{n}", payload={"sleep_ms": 10}) for n in range(4)]
    submission = await submit_batch(fleet_engine, label="b", jobs=jobs)
    process = start_worker(fleet_database_url, exit_when_idle=True)
    output, _ = await asyncio.to_thread(process.communicate, timeout=60)
    assert process.returncode == 0, output
    assert "w0: 4 published, 0 rejected, 0 lost, 0 released, 0 cancelled" in output
    states = {job.state for job in await batch_jobs(fleet_engine, submission.batch_id)}
    assert states == {"succeeded"}


async def test_a_cancelled_running_job_stops_and_releases_its_lease_at_once(
    fleet_engine: AsyncEngine,
) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=[NewJob(name="j", payload={})])
    job_id = submission.job_ids[0]
    running = asyncio.Event()
    interrupted = asyncio.Event()

    async def long_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            interrupted.set()
            raise
        return RunnerOutcome(outcome="succeeded", body={})

    # a 30 s lease, so a release at expiry would be far too late to pass
    worker = Worker(fleet_engine, long_job, worker_id="w", lease_seconds=30, heartbeat_seconds=0.05)
    stepping = asyncio.create_task(worker.step())
    await asyncio.wait_for(running.wait(), timeout=5)
    assert await cancel(fleet_engine, job_id) == "running"
    await asyncio.wait_for(stepping, timeout=5)
    assert interrupted.is_set()
    assert (worker.cancelled, worker.published, worker.lost) == (1, 0, 0)
    job = await job_status(fleet_engine, job_id)
    assert job is not None and (job.state, job.lease_expires_at) == ("cancelled", None)
    assert job.finished_at is not None and job.cancel_requested_at is not None
    assert (job.finished_at - job.cancel_requested_at).total_seconds() < 1
