"""The restart test again, with the real agent runner replaying the recorded task set."""

import random

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bench.jobs import Outcome
from bench.replay import RECORDINGS_ROOT, LatencyProfile, load_recordings
from bench.runner import FLEET_OUTCOMES, replay_payload
from bench.taskset import TASKSET_VERSION, load_taskset, plan_jobs
from fleet.models import NewJob
from fleet.store import batch_jobs, submit_batch
from tests.fleet_helpers import assert_every_job_accounted_for_once, kill_mid_batch_and_restart


@pytest.mark.parametrize("seed", [0, 1])
async def test_killing_a_worker_mid_agent_run_loses_and_duplicates_nothing(
    fleet_engine: AsyncEngine, fleet_database_url: str, seed: int
) -> None:
    taskset = load_taskset()
    recordings = load_recordings(RECORDINGS_ROOT / TASKSET_VERSION)
    planned = plan_jobs(taskset, len(taskset.tasks), seed=1)
    jobs = [
        NewJob(
            name=job.id,
            payload=replay_payload(
                taskset.get(job.task_id), recordings[job.task_id], LatencyProfile.ZERO
            ),
        )
        for job in planned
    ]
    submission = await submit_batch(fleet_engine, label=f"agents-{seed}", jobs=jobs)
    in_flight = await kill_mid_batch_and_restart(
        fleet_engine,
        fleet_database_url,
        submission,
        runner="bench.runner:run_job",
        rng=random.Random(seed),
        # agent jobs replay in about a second, so the lease leaves room without heartbeats
        lease_seconds=3.0,
        max_published_before_kill=12,
        kill_within_seconds=1.0,
    )
    await assert_every_job_accounted_for_once(fleet_engine, submission, in_flight)
    # and despite the kill, every agent did exactly what its recording says
    states = {job.name: job.state for job in await batch_jobs(fleet_engine, submission.batch_id)}
    expected = {job.id: FLEET_OUTCOMES[Outcome(recordings[job.task_id].outcome)] for job in planned}
    assert states == expected
