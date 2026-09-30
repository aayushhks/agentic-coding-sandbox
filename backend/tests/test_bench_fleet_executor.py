from sqlalchemy.ext.asyncio import AsyncEngine

from bench.fleet_executor import FleetExecutor
from bench.replay import LatencyProfile
from bench.runner import replay_payload
from bench.taskset import plan_jobs
from tests.bench_helpers import MINI_TASKSET, record_mini_batch


async def test_the_fleet_path_reproduces_the_recorded_outcomes(
    fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    recordings = await record_mini_batch()
    jobs = plan_jobs(MINI_TASKSET, 3, seed=1)
    batch = await FleetExecutor(fleet_database_url, lease_seconds=60).run(
        MINI_TASKSET,
        jobs,
        lambda task: replay_payload(task, recordings[task.id], LatencyProfile.ZERO),
    )
    assert batch.interrupted is None
    assert [result.job_id for result in batch.results] == [job.id for job in jobs]
    assert {result.task_id: result.outcome.value for result in batch.results} == {
        task_id: recording.outcome for task_id, recording in recordings.items()
    }
    for result in batch.results:
        assert (result.worker, result.attempts, result.divergence) == ("w0", 1, None)
        # every timestamp is Postgres's, measured from the moment the batch was inserted
        assert 0.0 == result.submitted_at <= result.claimed_at <= result.finished_at
