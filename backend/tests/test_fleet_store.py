import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob, RetryPolicy
from fleet.policy import ExecutionPolicy
from fleet.store import (
    IdempotencyConflictError,
    batch_jobs,
    batch_status,
    claim,
    job_result,
    job_status,
    publish,
    reap,
    release,
    start,
    submit_batch,
    unfinished_jobs,
)
from tests.fleet_helpers import NO_BACKOFF


def _jobs(count: int, tag: str = "") -> list[NewJob]:
    return [
        NewJob(name=f"job-{index}", payload={"index": index, "tag": tag}) for index in range(count)
    ]


async def _scalar(engine: AsyncEngine, sql: str) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text(sql)) or 0)


async def test_submit_enqueues_every_job_in_order(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(5))
    assert submission.created
    assert submission.job_ids == sorted(submission.job_ids)
    jobs = await batch_jobs(fleet_engine, submission.batch_id)
    assert [job.name for job in jobs] == [f"job-{index}" for index in range(5)]
    assert {job.state for job in jobs} == {"queued"}


async def test_the_same_key_and_jobs_return_the_original_batch(fleet_engine: AsyncEngine) -> None:
    first = await submit_batch(fleet_engine, label="b", jobs=_jobs(50), idempotency_key="k")
    again = await submit_batch(fleet_engine, label="b", jobs=_jobs(50), idempotency_key="k")
    assert (again.batch_id, again.job_ids, again.created) == (first.batch_id, first.job_ids, False)
    assert await _scalar(fleet_engine, "select count(*) from fleet_jobs") == 50


async def test_a_key_reused_for_different_jobs_is_refused(fleet_engine: AsyncEngine) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(3), idempotency_key="k")
    with pytest.raises(IdempotencyConflictError, match="different batch"):
        await submit_batch(fleet_engine, label="b", jobs=_jobs(3, tag="x"), idempotency_key="k")


async def test_concurrent_submissions_with_one_key_create_one_batch(
    fleet_engine: AsyncEngine,
) -> None:
    results = await asyncio.gather(
        *(
            submit_batch(fleet_engine, label="b", jobs=_jobs(50), idempotency_key="k")
            for _ in range(5)
        )
    )
    assert len({result.batch_id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    assert await _scalar(fleet_engine, "select count(*) from fleet_jobs") == 50


async def test_submissions_without_a_key_are_independent(fleet_engine: AsyncEngine) -> None:
    first = await submit_batch(fleet_engine, label="b", jobs=_jobs(2))
    second = await submit_batch(fleet_engine, label="b", jobs=_jobs(2))
    assert first.batch_id != second.batch_id


async def test_an_empty_batch_is_refused(fleet_engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="at least one job"):
        await submit_batch(fleet_engine, label="b", jobs=[])
    with pytest.raises(ValueError, match="at least one attempt"):
        await submit_batch(fleet_engine, label="b", jobs=_jobs(1), max_attempts=0)


async def test_a_batch_sets_how_many_attempts_each_job_gets(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(2), max_attempts=1)
    jobs = await batch_jobs(fleet_engine, submission.batch_id)
    assert {job.max_attempts for job in jobs} == {1}
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None
    # with a single attempt, the first infrastructure failure is final
    assert await release(fleet_engine, job_id=job.id, attempt=1, error="e") == "dead_lettered"


async def test_a_key_reused_with_a_different_retry_budget_is_refused(
    fleet_engine: AsyncEngine,
) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(2), idempotency_key="k")
    with pytest.raises(IdempotencyConflictError):
        await submit_batch(
            fleet_engine, label="b", jobs=_jobs(2), idempotency_key="k", max_attempts=5
        )


async def test_claims_take_the_oldest_job_under_a_lease(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(3))
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None
    assert (job.id, job.attempt, job.payload["index"]) == (submission.job_ids[0], 1, 0)
    status = await job_status(fleet_engine, job.id)
    assert status is not None
    assert (status.state, status.worker_id) == ("claimed", "w")
    assert status.lease_expires_at is not None and status.claimed_at is not None
    assert status.lease_expires_at > status.claimed_at
    assert await _scalar(fleet_engine, "select count(*) from fleet_attempts") == 1


async def test_claim_finds_nothing_in_an_empty_queue(fleet_engine: AsyncEngine) -> None:
    assert await claim(fleet_engine, worker_id="w", lease_seconds=60) is None


async def test_concurrent_claimers_never_share_a_job(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(60))

    async def drain(worker: str) -> list[int]:
        taken = []
        while (job := await claim(fleet_engine, worker_id=worker, lease_seconds=60)) is not None:
            taken.append(job.id)
        return taken

    claimed = await asyncio.gather(*(drain(f"w{index}") for index in range(3)))
    every = [job_id for taken in claimed for job_id in taken]
    assert sorted(every) == sorted(submission.job_ids)
    assert len(every) == len(set(every))


async def test_publishing_finishes_a_job_with_exactly_one_result(fleet_engine: AsyncEngine) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None
    assert await start(fleet_engine, job_id=job.id, attempt=job.attempt)
    assert await publish(
        fleet_engine,
        job_id=job.id,
        attempt=job.attempt,
        worker_id="w",
        outcome="succeeded",
        body={"ok": True},
    )
    assert not await publish(
        fleet_engine,
        job_id=job.id,
        attempt=job.attempt,
        worker_id="w",
        outcome="failed",
        body={"ok": False},
    )
    status = await job_status(fleet_engine, job.id)
    result = await job_result(fleet_engine, job.id)
    assert status is not None and result is not None
    assert (status.state, status.lease_expires_at, status.result_id) == ("succeeded", None, 1)
    assert (result.outcome, result.body, result.attempt) == ("succeeded", {"ok": True}, 1)
    assert await _scalar(fleet_engine, "select count(*) from fleet_results") == 1
    ended = await _scalar(
        fleet_engine, "select count(*) from fleet_attempts where ended_by = 'published'"
    )
    assert ended == 1


async def test_an_expired_attempt_cannot_publish_over_its_successor(
    fleet_engine: AsyncEngine,
) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    stale = await claim(fleet_engine, worker_id="slow", lease_seconds=0.05)
    assert stale is not None
    await asyncio.sleep(0.1)
    assert await reap(fleet_engine, retry=NO_BACKOFF) == [stale.id]
    fresh = await claim(fleet_engine, worker_id="fast", lease_seconds=60)
    assert fresh is not None and fresh.attempt == 2
    assert not await start(fleet_engine, job_id=stale.id, attempt=stale.attempt)
    assert not await publish(
        fleet_engine, job_id=stale.id, attempt=1, worker_id="slow", outcome="failed", body={}
    )
    assert await publish(
        fleet_engine, job_id=fresh.id, attempt=2, worker_id="fast", outcome="succeeded", body={}
    )
    result = await job_result(fleet_engine, fresh.id)
    assert result is not None and (result.attempt, result.worker_id) == (2, "fast")


async def test_a_lapsed_lease_cannot_publish_even_before_anyone_takes_the_job(
    fleet_engine: AsyncEngine,
) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    late = await claim(fleet_engine, worker_id="slow", lease_seconds=0.05)
    assert late is not None
    await asyncio.sleep(0.1)
    # nobody reaped or re-claimed it: the lease running out is enough to fence the attempt off
    assert not await start(fleet_engine, job_id=late.id, attempt=late.attempt)
    assert not await publish(
        fleet_engine, job_id=late.id, attempt=1, worker_id="slow", outcome="failed", body={}
    )
    status = await job_status(fleet_engine, late.id)
    assert status is not None and (status.state, status.result_id) == ("claimed", None)
    assert await _scalar(fleet_engine, "select count(*) from fleet_results") == 0
    assert await reap(fleet_engine) == [late.id]


async def test_a_result_is_stamped_with_the_moment_its_lease_was_checked(
    fleet_engine: AsyncEngine,
) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None
    assert await publish(
        fleet_engine, job_id=job.id, attempt=1, worker_id="w", outcome="succeeded", body={}
    )
    async with fleet_engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "select r.published_at, j.finished_at, a.ended_at, a.lease_expires_at "
                    "from fleet_results r join fleet_jobs j on j.id = r.job_id "
                    "join fleet_attempts a on a.job_id = r.job_id and a.attempt = r.attempt"
                )
            )
        ).one()
    assert row.published_at == row.finished_at == row.ended_at < row.lease_expires_at


async def test_reaping_requeues_only_expired_leases(fleet_engine: AsyncEngine) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(2))
    short = await claim(fleet_engine, worker_id="w", lease_seconds=0.05)
    long = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert short is not None and long is not None
    await asyncio.sleep(0.1)
    assert await reap(fleet_engine) == [short.id]
    requeued = await job_status(fleet_engine, short.id)
    kept = await job_status(fleet_engine, long.id)
    assert requeued is not None and kept is not None
    assert (requeued.state, requeued.worker_id, requeued.claimed_at) == ("queued", None, None)
    assert kept.state == "claimed"
    lapsed = await _scalar(
        fleet_engine, "select count(*) from fleet_attempts where ended_by = 'lease_expired'"
    )
    assert lapsed == 1
    assert await reap(fleet_engine) == []


async def test_batch_status_counts_states_until_done(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="demo", jobs=_jobs(2))
    status = await batch_status(fleet_engine, submission.batch_id)
    assert status is not None
    assert (status.label, status.total, status.counts, status.done) == (
        "demo",
        2,
        {"queued": 2},
        False,
    )
    assert await unfinished_jobs(fleet_engine) == 2
    while (job := await claim(fleet_engine, worker_id="w", lease_seconds=60)) is not None:
        await publish(
            fleet_engine,
            job_id=job.id,
            attempt=job.attempt,
            worker_id="w",
            outcome="failed",
            body={},
        )
    done = await batch_status(fleet_engine, submission.batch_id)
    assert done is not None and (done.counts, done.done) == ({"failed": 2}, True)
    assert await unfinished_jobs(fleet_engine) == 0


async def test_reads_of_missing_things_return_none(fleet_engine: AsyncEngine) -> None:
    assert await batch_status(fleet_engine, 99) is None
    assert await job_status(fleet_engine, 99) is None
    assert await job_result(fleet_engine, 99) is None


async def _attempt_log(engine: AsyncEngine) -> list[tuple[int, str, str | None, float]]:
    """Each attempt's number, ending and error, and how long its job then had to wait."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "select a.attempt, a.ended_by, a.error, "
                "extract(epoch from j.available_at - a.ended_at) as wait "
                "from fleet_attempts a join fleet_jobs j on j.id = a.job_id order by a.attempt"
            )
        )
        return [(row.attempt, row.ended_by, row.error, float(row.wait)) for row in rows]


async def test_a_released_job_waits_out_its_backoff_before_it_is_claimed_again(
    fleet_engine: AsyncEngine,
) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None
    retry = RetryPolicy(backoff_seconds=0.3)
    state = await release(fleet_engine, job_id=job.id, attempt=1, error="boom", retry=retry)
    assert state == "queued"
    assert await claim(fleet_engine, worker_id="w", lease_seconds=60) is None
    await asyncio.sleep(0.35)
    again = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert again is not None and (again.id, again.attempt) == (submission.job_ids[0], 2)
    status = await job_status(fleet_engine, job.id)
    assert status is not None and status.last_error == "boom"


async def test_each_retry_waits_twice_as_long_up_to_the_cap(fleet_engine: AsyncEngine) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    retry = RetryPolicy(backoff_seconds=0.1, backoff_cap_seconds=0.15)
    for attempt in (1, 2):
        await asyncio.sleep(0.2)
        job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
        assert job is not None and job.attempt == attempt
        assert await release(fleet_engine, job_id=job.id, attempt=attempt, error="e", retry=retry)
    waits = [wait for _attempt, _ended, _error, wait in await _attempt_log(fleet_engine)]
    # the job's latest wait: 0.1 s after the first failure, doubled to 0.2 but capped at 0.15
    assert waits[-1] == pytest.approx(0.15, abs=1e-6)


async def test_a_job_that_uses_every_attempt_is_dead_lettered(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    states = []
    for attempt in (1, 2, 3):
        job = await claim(fleet_engine, worker_id=f"w{attempt}", lease_seconds=60)
        assert job is not None and job.attempt == attempt
        states.append(
            await release(
                fleet_engine, job_id=job.id, attempt=attempt, error=f"e{attempt}", retry=NO_BACKOFF
            )
        )
    assert states == ["queued", "queued", "dead_lettered"]
    status = await job_status(fleet_engine, submission.job_ids[0])
    assert status is not None
    assert (status.state, status.result_id, status.lease_expires_at) == (
        "dead_lettered",
        None,
        None,
    )
    assert (status.worker_id, status.last_error) == ("w3", "e3")
    assert status.finished_at is not None
    assert await claim(fleet_engine, worker_id="w", lease_seconds=60) is None
    done = await batch_status(fleet_engine, submission.batch_id)
    assert done is not None and (done.counts, done.done) == ({"dead_lettered": 1}, True)
    log = await _attempt_log(fleet_engine)
    assert [(n, ended, error) for n, ended, error, _wait in log] == [
        (1, "released", "e1"),
        (2, "released", "e2"),
        (3, "released", "e3"),
    ]


async def test_lapsed_leases_use_up_attempts_too(fleet_engine: AsyncEngine) -> None:
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    for _ in range(3):
        assert await claim(fleet_engine, worker_id="w", lease_seconds=0.01) is not None
        await asyncio.sleep(0.05)
        assert await reap(fleet_engine, retry=NO_BACKOFF) == submission.job_ids
    status = await job_status(fleet_engine, submission.job_ids[0])
    assert status is not None and (status.state, status.attempt) == ("dead_lettered", 3)
    assert status.last_error == "lease expired"


async def test_a_release_needs_a_live_lease_on_the_current_attempt(
    fleet_engine: AsyncEngine,
) -> None:
    await submit_batch(fleet_engine, label="b", jobs=_jobs(1))
    late = await claim(fleet_engine, worker_id="slow", lease_seconds=0.05)
    assert late is not None
    await asyncio.sleep(0.1)
    assert await release(fleet_engine, job_id=late.id, attempt=1, error="e") is None
    assert await reap(fleet_engine, retry=NO_BACKOFF) == [late.id]
    successor = await claim(fleet_engine, worker_id="fast", lease_seconds=60)
    assert successor is not None and successor.attempt == 2
    assert await release(fleet_engine, job_id=late.id, attempt=1, error="e") is None
    status = await job_status(fleet_engine, late.id)
    assert status is not None and (status.state, status.worker_id) == ("claimed", "fast")


async def test_each_job_keeps_its_batchs_policy_and_is_claimed_with_it(
    fleet_engine: AsyncEngine,
) -> None:
    policy = ExecutionPolicy(memory_mb=256, timeout_seconds=30, egress=("api.groq.com:443",))
    submission = await submit_batch(fleet_engine, label="b", jobs=_jobs(2), policy=policy)
    assert {job.policy for job in await batch_jobs(fleet_engine, submission.batch_id)} == {policy}
    job = await claim(fleet_engine, worker_id="w", lease_seconds=60)
    assert job is not None and job.policy == policy
    # the same key and jobs under a different policy is a different batch
    await submit_batch(fleet_engine, label="b", jobs=_jobs(2), idempotency_key="k")
    with pytest.raises(IdempotencyConflictError):
        await submit_batch(
            fleet_engine, label="b", jobs=_jobs(2), idempotency_key="k", policy=policy
        )
