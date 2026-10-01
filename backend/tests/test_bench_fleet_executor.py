import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bench.fleet_executor import FleetExecutor, pool_topology
from bench.replay import LatencyProfile
from bench.runner import replay_payload
from bench.taskset import plan_jobs
from tests.bench_helpers import MINI_TASKSET, record_mini_batch


@pytest.mark.parametrize("workers", [1, 2])
async def test_the_fleet_path_reproduces_the_recorded_outcomes(
    fleet_engine: AsyncEngine, fleet_database_url: str, workers: int
) -> None:
    recordings = await record_mini_batch()
    jobs = plan_jobs(MINI_TASKSET, 3, seed=1)
    fleet = FleetExecutor(fleet_database_url, workers=workers, lease_seconds=60)
    batch = await fleet.run(
        MINI_TASKSET,
        jobs,
        lambda task: replay_payload(task, recordings[task.id], LatencyProfile.ZERO),
    )
    assert batch.interrupted is None
    assert [result.job_id for result in batch.results] == [job.id for job in jobs]
    assert {result.task_id: result.outcome.value for result in batch.results} == {
        task_id: recording.outcome for task_id, recording in recordings.items()
    }
    pool = {f"w{index}" for index in range(workers)}
    for result in batch.results:
        assert result.worker in pool
        assert (result.attempts, result.divergence) == (1, None)
        # every timestamp is Postgres's, measured from the moment the batch was inserted
        assert 0.0 == result.submitted_at <= result.claimed_at <= result.finished_at
    # the agent ran in the workers' processes, so their trees used cpu the host saw
    used = batch.resources
    assert used is not None and used.window_seconds > 0
    workers_cpu = used.processes["workers"]
    assert workers_cpu is not None and 0 < workers_cpu <= used.host_busy_cpu_seconds + 0.1
    assert used.processes["api"] is not None
    # the database's server pid was not given, so its cpu goes unmeasured
    assert used.processes["postgres"] is None
    assert batch.database is not None
    assert batch.database["publish"].calls == 3
    assert batch.database["claim"].calls == 3


def test_a_fleet_needs_a_worker_and_describes_its_pool() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        FleetExecutor("postgresql+asyncpg://unused", workers=0)
    assert FleetExecutor("postgresql+asyncpg://unused", workers=4).topology == pool_topology(4)
    assert pool_topology(1).startswith("single host: one fleet worker process, ")
