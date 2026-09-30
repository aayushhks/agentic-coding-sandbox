"""Kill the worker mid-batch with SIGKILL, restart it, and account for every job exactly once."""

import random

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.models import NewJob
from fleet.store import batch_jobs, submit_batch
from tests.fleet_helpers import assert_every_job_accounted_for_once, kill_mid_batch_and_restart


@pytest.mark.parametrize("seed", range(10))
async def test_a_killed_worker_loses_and_duplicates_nothing(
    fleet_engine: AsyncEngine, fleet_database_url: str, seed: int
) -> None:
    rng = random.Random(seed)
    jobs = [NewJob(name=f"job-{n}", payload={"sleep_ms": rng.randint(10, 60)}) for n in range(50)]
    submission = await submit_batch(
        fleet_engine, label=f"restart-{seed}", jobs=jobs, idempotency_key=f"restart-{seed}"
    )
    in_flight = await kill_mid_batch_and_restart(
        fleet_engine,
        fleet_database_url,
        submission,
        runner="tests.fleet_helpers:sleep_runner",
        rng=rng,
        # short, so a dead worker's job comes back quickly; still longer than any job here
        lease_seconds=1.0,
        max_published_before_kill=45,
        kill_within_seconds=0.06,
    )
    await assert_every_job_accounted_for_once(fleet_engine, submission, in_flight)
    assert {job.state for job in await batch_jobs(fleet_engine, submission.batch_id)} == {
        "succeeded"
    }
