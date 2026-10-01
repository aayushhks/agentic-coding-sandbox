"""A worker rides through a dropped database connection, and a call made twice counts once."""

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.connections import connection_lost, ride_through
from fleet.failpoints import arm
from fleet.models import NewJob
from fleet.runners import RunnerOutcome
from fleet.store import batch_jobs, cancel, claim, job_result, job_status, submit_batch
from fleet.worker import Worker, worker_engine
from tests.fleet_helpers import NO_BACKOFF, sleep_runner


@pytest.fixture(autouse=True)
def _disarmed() -> Iterator[None]:
    yield
    arm("")


class _ServerError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def test_only_a_connection_that_went_away_counts_as_lost() -> None:
    for lost in (
        ConnectionRefusedError(),
        ConnectionResetError(),
        TimeoutError(),
        _ServerError("08003"),
        _ServerError("57P01"),
        _ServerError("57P03"),
        DBAPIError("select 1", None, _ServerError("XX000"), connection_invalidated=True),
        DBAPIError("select 1", None, _ServerError("08006")),
    ):
        assert connection_lost(lost), lost
    for failed in (
        ValueError(),
        _ServerError("23505"),
        # a statement timeout cancels the query, and the connection lives on
        _ServerError("57014"),
        DBAPIError("insert", None, _ServerError("23505")),
    ):
        assert not connection_lost(failed), failed


async def test_a_call_is_made_again_while_its_connection_keeps_dropping() -> None:
    attempts: list[int] = []
    slept: list[float] = []

    async def call() -> int:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise ConnectionResetError("dropped")
        return 7

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    assert await ride_through(call, seconds=10, sleep=sleep) == 7
    assert (len(attempts), slept) == (3, [0.05, 0.1])


async def test_it_gives_up_when_its_time_is_spent_and_never_retries_a_failed_call() -> None:
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    async def dropping() -> None:
        raise ConnectionResetError("dropped")

    with pytest.raises(ConnectionResetError):
        await ride_through(dropping, seconds=1.0, sleep=sleep, clock=lambda: now[0])
    assert 0.5 < now[0] <= 1.0
    calls = []

    async def failing() -> None:
        calls.append(1)
        raise ValueError("the call itself failed")

    with pytest.raises(ValueError):
        await ride_through(failing, seconds=10, sleep=sleep)
    assert calls == [1]


def _worker(engine: AsyncEngine, runner: Any = sleep_runner, **options: Any) -> Worker:
    return Worker(
        engine,
        runner,
        worker_id="w",
        lease_seconds=5,
        heartbeat_seconds=0.1,
        retry=NO_BACKOFF,
        **options,
    )


@pytest.mark.parametrize(
    "point",
    [
        "claim.before",
        "claim.before_commit",
        "start.after_commit",
        "heartbeat.before",
        "publish.before_commit",
        "publish.after_commit",
    ],
)
async def test_a_dropped_connection_anywhere_in_a_job_costs_nothing_but_a_retry(
    fleet_engine: AsyncEngine, point: str
) -> None:
    submission = await submit_batch(
        fleet_engine, label=point, jobs=[NewJob(name="j", payload={"sleep_ms": 250})]
    )
    arm(f"{point}=drop@1")
    worker = _worker(fleet_engine)
    assert await worker.run(exit_when_idle=True) == 1
    # one attempt, one result from it, and a worker whose counts say what happened
    result = await job_result(fleet_engine, submission.job_ids[0])
    assert result is not None and (result.attempt, result.worker_id) == (1, "w")
    assert (worker.published, worker.rejected, worker.lost) == (1, 0, 0)


async def test_a_lost_answer_to_a_claim_strands_the_job_until_its_lease_runs_out(
    fleet_engine: AsyncEngine,
) -> None:
    submission = await submit_batch(
        fleet_engine, label="lost claim", jobs=[NewJob(name="j", payload={})]
    )
    arm("claim.after_commit=drop@1")
    worker = Worker(
        fleet_engine,
        sleep_runner,
        worker_id="w",
        lease_seconds=0.5,
        retry=NO_BACKOFF,
        reap_every_seconds=0.1,
    )
    assert await worker.run(exit_when_idle=True) == 1
    # the claim committed but its answer was lost, so that attempt held the job for nothing
    result = await job_result(fleet_engine, submission.job_ids[0])
    assert result is not None and result.attempt == 2


async def _crash(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    raise RuntimeError("crashed underneath the task")


@pytest.mark.parametrize("side", ["before_commit", "after_commit"])
async def test_a_job_given_back_over_a_dropped_connection_is_given_back_once(
    fleet_engine: AsyncEngine, side: str
) -> None:
    submission = await submit_batch(
        fleet_engine, label="released", jobs=[NewJob(name="j", payload={})], max_attempts=2
    )
    arm(f"release.{side}=drop@1")
    worker = _worker(fleet_engine, _crash)
    await worker.run(exit_when_idle=True)
    (job,) = await batch_jobs(fleet_engine, submission.batch_id)
    assert (job.state, job.attempt, worker.released, worker.lost) == ("dead_lettered", 2, 2, 0)


@pytest.mark.parametrize("side", ["before_commit", "after_commit"])
async def test_a_cancel_finished_over_a_dropped_connection_counts_once(
    fleet_engine: AsyncEngine, side: str
) -> None:
    submission = await submit_batch(
        fleet_engine, label="cancelled", jobs=[NewJob(name="j", payload={"sleep_ms": 5000})]
    )
    job_id = submission.job_ids[0]
    arm(f"cancel.{side}=drop@1")
    worker = _worker(fleet_engine)
    working = asyncio.create_task(worker.run(exit_when_idle=True))
    deadline = time.monotonic() + 30
    while (status := await job_status(fleet_engine, job_id)) is None or status.state != "running":
        assert time.monotonic() < deadline, "the job never started"
        await asyncio.sleep(0.01)
    # the next heartbeat sees the cancel, and the worker ends the job over a dropped connection
    await cancel(fleet_engine, job_id)
    await asyncio.wait_for(working, timeout=30)
    (job,) = await batch_jobs(fleet_engine, submission.batch_id)
    assert (job.state, worker.cancelled, worker.lost) == ("cancelled", 1, 0)


async def test_a_worker_frozen_inside_a_transaction_lets_go_of_its_job_after_a_lease(
    fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    submission = await submit_batch(
        fleet_engine, label="frozen", jobs=[NewJob(name="j", payload={})]
    )
    frozen = worker_engine(fleet_database_url, lease_seconds=0.5)
    try:
        with pytest.raises(DBAPIError) as ended:
            async with frozen.begin() as holding:
                await holding.execute(text("select id from fleet_jobs for update"))
                # while the frozen worker holds the job's row, every claim passes it by
                assert await claim(fleet_engine, worker_id="other", lease_seconds=5) is None
                await asyncio.sleep(1.0)
                # half a second idle in its transaction, and Postgres ended the session
                taken = await claim(fleet_engine, worker_id="other", lease_seconds=5)
        assert taken is not None and taken.id == submission.job_ids[0]
        # and the frozen worker, if it ever wakes, finds a dropped connection to ride through
        assert connection_lost(ended.value)
    finally:
        await frozen.dispose()
