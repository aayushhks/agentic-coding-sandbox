"""The fleet runner for bench jobs: a self-contained payload in, the task's execution out.

A replay job carries its recording; a real job names the model, and the worker calls it with the
key from its own environment, never from the payload, and sends back every response it got.
"""

from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from app.agent.types import AgentStep
from app.core.config import get_settings
from app.llm.base import LLMProvider
from app.llm.groq_provider import GroqProvider
from bench.executor import TaskExecution, agent_configs, execute_task, step_event
from bench.groq_limits import retry_delay
from bench.jobs import Outcome
from bench.records import utc_now
from bench.replay import (
    LatencyProfile,
    Recording,
    RecordingProvider,
    ReplayProvider,
    build_recording,
)
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

    # payloads made before real jobs existed have no kind, and are replays
    kind: Literal["replay"] = "replay"
    task: BenchTask
    recording: Recording
    latency: LatencyProfile
    # rules the experiment adds to the agent's system prompt
    extra_rules: list[str] = []


class RealJob(BaseModel):
    """A bench task to run against the real model; the payload holds no credentials."""

    kind: Literal["real"] = "real"
    task: BenchTask
    model: str
    taskset_version: str
    # the bench's checkout, which its workers share, so the recording says what made it
    git_sha: str
    extra_rules: list[str] = []


def replay_payload(
    task: BenchTask,
    recording: Recording,
    latency: LatencyProfile,
    extra_rules: Sequence[str] = (),
) -> dict[str, Any]:
    job = ReplayJob(task=task, recording=recording, latency=latency, extra_rules=list(extra_rules))
    return job.model_dump(mode="json")


def real_payload(
    task: BenchTask,
    model: str,
    *,
    taskset_version: str,
    git_sha: str,
    extra_rules: Sequence[str] = (),
) -> dict[str, Any]:
    job = RealJob(
        task=task,
        model=model,
        taskset_version=taskset_version,
        git_sha=git_sha,
        extra_rules=list(extra_rules),
    )
    return job.model_dump(mode="json")


def real_provider(model: str) -> LLMProvider:
    """The model a real job calls, with the key from this worker's own environment."""
    key = get_settings().groq_api_key
    if not key:
        # raised, not returned: an infrastructure failure, which the fleet retries
        raise RuntimeError("GROQ_API_KEY is not set where this worker runs")
    # the recorder owns retries, so each call's latency leaves out rate-limit waits
    return GroqProvider(key, model=model, max_retries=0)


def _with_waits(provider: RecordingProvider) -> Callable[[AgentStep], None]:
    """Report each step with the time its model calls took and waited out the rate limit, so a
    job stopped before it returns still says where its time went."""
    reported = 0

    def on_step(step: AgentStep) -> None:
        nonlocal reported
        calls = provider.calls[reported:]
        reported = len(provider.calls)
        report(
            step_event(step)
            | {
                "model_seconds": sum(call.latency_seconds for call in calls),
                "retry_wait_seconds": sum(call.retry_wait_seconds for call in calls),
            }
        )

    return on_step


async def _run_real(job: RealJob) -> RunnerOutcome:
    provider = RecordingProvider(real_provider(job.model), retry_delay=retry_delay)
    execution = await execute_task(
        job.task,
        provider,
        configs=agent_configs(job.extra_rules),
        on_step=_with_waits(provider),
    )
    recording = build_recording(
        provider,
        task_id=job.task.id,
        taskset_version=job.taskset_version,
        outcome=execution.outcome.value,
        git_sha=job.git_sha,
        recorded_at=utc_now(),
    )
    body = execution.model_dump(mode="json") | {"recording": recording.model_dump(mode="json")}
    return RunnerOutcome(outcome=FLEET_OUTCOMES[execution.outcome], body=body)


async def run_job(name: str, payload: dict[str, Any]) -> RunnerOutcome:
    if payload.get("kind") == "real":
        return await _run_real(RealJob.model_validate(payload))
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


def recording_from_body(body: dict[str, Any]) -> Recording | None:
    """The responses a real job got, which a replay job has none of."""
    raw = body.get("recording")
    return None if raw is None else Recording.model_validate(raw)
