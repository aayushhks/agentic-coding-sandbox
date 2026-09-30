"""The invariant checker passes a clean run and catches each kind of corruption fed to it."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.invariants import (
    AttemptRow,
    Invariant,
    JobRow,
    ResultRow,
    Snapshot,
    check,
    snapshot,
)
from fleet.models import NewJob
from fleet.store import claim, submit_batch
from fleet.worker import RunnerOutcome, Worker
from tests.fleet_helpers import NO_BACKOFF

START = datetime(2026, 1, 1, tzinfo=UTC)
SUBMITTED = [1, 2, 3]


def _at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def _attempt(job: int, number: int, claimed: float, ended: float, how: str) -> AttemptRow:
    return AttemptRow(
        job_id=job,
        attempt=number,
        worker_id=f"w{number}",
        claimed_at=_at(claimed),
        lease_expires_at=_at(claimed + 30),
        ended_at=_at(ended),
        ended_by=how,
    )


def _clean() -> Snapshot:
    """Published at once; published after a lapsed lease; dead-lettered after three crashes."""
    return Snapshot(
        jobs=[
            JobRow(id=1, state="succeeded", attempt=1, max_attempts=3, result_id=1),
            JobRow(id=2, state="failed", attempt=2, max_attempts=3, result_id=2),
            JobRow(id=3, state="dead_lettered", attempt=3, max_attempts=3, result_id=None),
        ],
        attempts=[
            _attempt(1, 1, 0, 5, "published"),
            _attempt(2, 1, 0, 31, "lease_expired"),
            _attempt(2, 2, 33, 40, "published"),
            _attempt(3, 1, 0, 1, "released"),
            _attempt(3, 2, 3, 4, "released"),
            _attempt(3, 3, 8, 9, "released"),
        ],
        results=[
            ResultRow(
                id=1, job_id=1, attempt=1, worker_id="w1", published_at=_at(5), outcome="succeeded"
            ),
            ResultRow(
                id=2, job_id=2, attempt=2, worker_id="w2", published_at=_at(40), outcome="failed"
            ),
        ],
    )


def _with(run: Snapshot, table: str, index: int, **changes: Any) -> Snapshot:
    rows = list(getattr(run, table))
    rows[index] = rows[index].model_copy(update=changes)
    return run.model_copy(update={table: rows})


def _extra(run: Snapshot, table: str, row: Any) -> Snapshot:
    return run.model_copy(update={table: [*getattr(run, table), row]})


def _without(run: Snapshot, table: str, index: int) -> Snapshot:
    rows = list(getattr(run, table))
    del rows[index]
    return run.model_copy(update={table: rows})


def test_a_clean_run_breaks_nothing() -> None:
    assert check(_clean(), SUBMITTED) == []


# one corruption per line; each must be caught under the invariant it breaks
CORRUPTIONS: list[tuple[str, Invariant, Callable[[Snapshot], Snapshot]]] = [
    (
        "a second result for a job",
        "one_result",
        lambda run: _extra(
            run,
            "results",
            ResultRow(
                id=9, job_id=1, attempt=1, worker_id="w1", published_at=_at(6), outcome="failed"
            ),
        ),
    ),
    ("a finished job with no result", "one_result", lambda run: _without(run, "results", 0)),
    (
        "a result the job disagrees with",
        "one_result",
        lambda r: _with(r, "results", 0, outcome="failed"),
    ),
    ("a job left running", "nothing_lost", lambda r: _with(r, "jobs", 0, state="running")),
    ("a job that vanished", "nothing_lost", lambda run: _without(run, "jobs", 2)),
    (
        "a result published after its lease ran out",
        "no_stale_write",
        lambda run: _with(run, "results", 0, published_at=_at(31)),
    ),
    (
        "a result from a superseded attempt",
        "no_stale_write",
        lambda run: _with(run, "results", 1, attempt=1, worker_id="w1"),
    ),
    (
        "a result from a worker that didn't hold the attempt",
        "no_stale_write",
        lambda run: _with(run, "results", 0, worker_id="intruder"),
    ),
    (
        "an attempt claimed while the one before still held the job",
        "no_stale_write",
        lambda run: _with(run, "attempts", 2, claimed_at=_at(20)),
    ),
    ("a gap in the attempt log", "accounting", lambda run: _without(run, "attempts", 4)),
    (
        "a lease reaped before it ran out",
        "accounting",
        lambda run: _with(run, "attempts", 1, ended_at=_at(29)),
    ),
    (
        "an attempt released after its lease ran out",
        "accounting",
        lambda run: _with(run, "attempts", 3, ended_at=_at(31)),
    ),
    (
        "an attempt that never ended",
        "accounting",
        lambda r: _with(r, "attempts", 5, ended_at=None, ended_by=None),
    ),
    (
        "a dead letter with attempts to spare",
        "accounting",
        lambda run: _with(run, "jobs", 2, max_attempts=5),
    ),
    (
        "a job retried after it published",
        "accounting",
        lambda run: _with(run, "attempts", 1, ended_by="published"),
    ),
    ("more attempts than the budget", "accounting", lambda r: _with(r, "jobs", 2, max_attempts=2)),
]


@pytest.mark.parametrize(
    ("corruption", "invariant", "corrupt"),
    CORRUPTIONS,
    ids=[corruption for corruption, _invariant, _corrupt in CORRUPTIONS],
)
def test_each_corruption_is_caught_under_the_invariant_it_breaks(
    corruption: str, invariant: Invariant, corrupt: Callable[[Snapshot], Snapshot]
) -> None:
    violations = check(corrupt(_clean()), SUBMITTED)
    assert invariant in {violation.invariant for violation in violations}, violations


def test_every_invariant_has_a_corruption_that_breaks_it() -> None:
    assert {invariant for _c, invariant, _f in CORRUPTIONS} == {
        "one_result",
        "nothing_lost",
        "no_stale_write",
        "accounting",
    }


def test_totals_that_do_not_add_up_are_caught() -> None:
    violations = check(_clean(), [*SUBMITTED, 4])
    assert [(v.invariant, v.job_id) for v in violations] == [
        ("nothing_lost", 4),
        ("accounting", None),
    ]


async def _mixed_run(engine: AsyncEngine) -> list[int]:
    """A real run with a published job, a task failure, a dead letter and a lapsed lease."""

    async def runner(name: str, payload: dict[str, Any]) -> RunnerOutcome:
        if name == "crashes":
            raise RuntimeError("boom")
        return RunnerOutcome(outcome="failed" if name == "gives-up" else "succeeded", body={})

    names = ["runs", "gives-up", "crashes", "abandoned"]
    submission = await submit_batch(
        engine, label="mixed", jobs=[NewJob(name=name, payload={}) for name in names]
    )
    # the first job in the queue is claimed by a worker that dies holding it
    assert await claim(engine, worker_id="dies", lease_seconds=0.1) is not None
    await asyncio.sleep(0.15)
    worker = Worker(engine, runner, worker_id="w", lease_seconds=5, retry=NO_BACKOFF)
    await asyncio.wait_for(worker.run(exit_when_idle=True), timeout=20)
    return submission.job_ids


async def test_a_real_run_with_every_kind_of_ending_passes(fleet_engine: AsyncEngine) -> None:
    job_ids = await _mixed_run(fleet_engine)
    run = await snapshot(fleet_engine, job_ids)
    assert sorted(job.state for job in run.jobs) == [
        "dead_lettered",
        "failed",
        "succeeded",
        "succeeded",
    ]
    assert check(run, job_ids) == []


async def test_corruption_written_to_the_database_is_caught(fleet_engine: AsyncEngine) -> None:
    job_ids = await _mixed_run(fleet_engine)
    async with fleet_engine.begin() as connection:
        # as if a publish had skipped the lease check an hour after its lease ran out
        await connection.execute(
            text("update fleet_results set published_at = published_at + interval '1 hour'")
        )
    violations = check(await snapshot(fleet_engine, job_ids), job_ids)
    assert {violation.invariant for violation in violations} == {"no_stale_write"}
    assert len({violation.job_id for violation in violations}) == 3
