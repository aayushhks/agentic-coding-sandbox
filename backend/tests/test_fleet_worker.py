import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob
from fleet.store import batch_jobs, claim, job_result, submit_batch
from fleet.worker import RunnerOutcome, Worker, load_runner
from tests.fleet_helpers import sleep_runner, start_worker


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


async def test_a_runner_crash_is_recorded_as_that_jobs_failure(fleet_engine: AsyncEngine) -> None:
    jobs = [NewJob(name=f"j{n}", payload={"n": n}) for n in (3, 4)]
    submission = await submit_batch(fleet_engine, label="b", jobs=jobs)
    await _worker(fleet_engine).run(exit_when_idle=True)
    states = [job.state for job in await batch_jobs(fleet_engine, submission.batch_id)]
    assert states == ["failed", "succeeded"]
    crashed = await job_result(fleet_engine, submission.job_ids[0])
    assert crashed is not None and crashed.body == {"error": "RuntimeError: boom"}


async def test_exiting_when_idle_waits_for_an_abandoned_job(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(
        fleet_engine, label="b", jobs=[NewJob(name="j", payload={"n": 1})]
    )
    # a worker that claimed the job and died: its lease simply runs out
    abandoned = await claim(fleet_engine, worker_id="dead", lease_seconds=0.2)
    assert abandoned is not None
    worker = _worker(fleet_engine, reap_every_seconds=0.05, min_poll_seconds=0.02)
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
    assert "w0: 4 published, 0 rejected" in output
    states = {job.state for job in await batch_jobs(fleet_engine, submission.batch_id)}
    assert states == {"succeeded"}
