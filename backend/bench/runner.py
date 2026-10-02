"""The fleet runner for bench jobs: a self-contained replay payload in, the task's execution out."""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from bench.executor import TaskExecution, agent_configs, execute_task, step_event
from bench.jobs import Outcome
from bench.replay import LatencyProfile, Recording, ReplayProvider
from bench.taskset import BenchTask
from fleet.models import Outcome as FleetOutcome
from fleet.progress import report
from fleet.runners import RunnerOutcome

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
    # rules the experiment adds to the agent's system prompt
    extra_rules: list[str] = []


def replay_payload(
    task: BenchTask,
    recording: Recording,
    latency: LatencyProfile,
    extra_rules: Sequence[str] = (),
) -> dict[str, Any]:
    job = ReplayJob(task=task, recording=recording, latency=latency, extra_rules=list(extra_rules))
    return job.model_dump(mode="json")


async def run_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    job = ReplayJob.model_validate(payload)
    execution = await execute_task(
        job.task,
        ReplayProvider(job.recording, latency=job.latency),
        configs=agent_configs(job.extra_rules),
        on_step=lambda step: report(step_event(step)),
    )
    return RunnerOutcome(
        outcome=FLEET_OUTCOMES[execution.outcome], body=execution.model_dump(mode="json")
    )


def execution_from_body(body: dict[str, Any]) -> TaskExecution:
    return TaskExecution.model_validate(body)
