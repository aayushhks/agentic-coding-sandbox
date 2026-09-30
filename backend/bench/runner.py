"""The fleet runner for bench jobs: a self-contained replay payload in, the task's execution out."""

from typing import Any

from pydantic import BaseModel

from bench.executor import TaskExecution, execute_task
from bench.jobs import Outcome
from bench.replay import LatencyProfile, Recording, ReplayProvider
from bench.taskset import BenchTask
from fleet.models import Outcome as FleetOutcome
from fleet.worker import RunnerOutcome

FLEET_OUTCOMES: dict[Outcome, FleetOutcome] = {
    Outcome.SOLVED: "succeeded",
    Outcome.ESCALATED: "escalated",
    Outcome.FAILED: "failed",
}


class ReplayJob(BaseModel):
    """Everything a worker needs to run one bench task, with nothing read from the repo."""

    task: BenchTask
    recording: Recording
    latency: LatencyProfile


def replay_payload(
    task: BenchTask, recording: Recording, latency: LatencyProfile
) -> dict[str, Any]:
    return ReplayJob(task=task, recording=recording, latency=latency).model_dump(mode="json")


async def run_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    job = ReplayJob.model_validate(payload)
    execution = await execute_task(job.task, ReplayProvider(job.recording, latency=job.latency))
    return RunnerOutcome(
        outcome=FLEET_OUTCOMES[execution.outcome], body=execution.model_dump(mode="json")
    )


def execution_from_body(body: dict[str, Any]) -> TaskExecution:
    return TaskExecution.model_validate(body)
