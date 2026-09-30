"""Run bench jobs through today's single-process execution path."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from app.agent.types import AgentConfig, AgentRun, AgentStep, StepCallback, TerminationReason
from app.benchmark.runner import TaskResult, run_task
from app.eval.failure import FailureMode, classify_failure
from app.llm.base import LLMProvider
from app.tickets.runner import ResolutionOutcome, TicketResolution, resolve_ticket
from bench.jobs import FailureKind, JobResult, Outcome
from bench.replay import RecordingProvider, ReplayProvider
from bench.taskset import BenchTask, Expectation, Job, TaskKind, TaskSet

# the m7 settings that took the benchmark to 15/15; tickets also get the escalate tool
AGENT_CONFIGS: dict[TaskKind, AgentConfig] = {
    TaskKind.BENCHMARK: AgentConfig(require_verified_finish=True),
    TaskKind.TICKET: AgentConfig(allow_escalation=True, require_verified_finish=True),
}
REPLAY_DIVERGENCE = "replay_divergence"
_INFRA_MODES = {FailureMode.PROVIDER_ERROR.value, FailureMode.SANDBOX_ERROR.value}

ProviderFactory = Callable[[BenchTask], LLMProvider]
# checked after every job; a returned reason stops the batch and drops that job's result
StopCheck = Callable[[LLMProvider], str | None]
JobCallback = Callable[[BenchTask, LLMProvider, JobResult], None]
Clock = Callable[[], float]


@dataclass(slots=True)
class BatchResult:
    results: list[JobResult]
    interrupted: str | None


class Executor(Protocol):
    name: str
    topology: str
    workers: int

    async def run(
        self,
        taskset: TaskSet,
        jobs: Sequence[Job],
        provider_for: ProviderFactory,
        *,
        stop_check: StopCheck | None = None,
        on_result: JobCallback | None = None,
        clock: Clock = time.monotonic,
    ) -> BatchResult: ...


def _benchmark_outcome(result: TaskResult) -> tuple[Outcome, str | None]:
    if result.solved:
        return Outcome.SOLVED, None
    if result.run.escalated:
        return Outcome.ESCALATED, None
    mode = classify_failure(result) or FailureMode.WRONG_SOLUTION
    return Outcome.FAILED, mode.value


def _unresolved_mode(run: AgentRun) -> str:
    if run.termination_reason == TerminationReason.PROVIDER_ERROR:
        return FailureMode.PROVIDER_ERROR.value
    if run.termination_reason == TerminationReason.MALFORMED_LIMIT:
        return FailureMode.MALFORMED_TOOL_CALL.value
    if any("sandbox error" in step.observation for step in run.steps):
        return FailureMode.SANDBOX_ERROR.value
    return FailureMode.EXHAUSTED_ITERATIONS.value


def ticket_outcome(resolution: TicketResolution) -> tuple[Outcome, str | None]:
    match resolution.outcome:
        case ResolutionOutcome.RESOLVED:
            return Outcome.SOLVED, None
        case ResolutionOutcome.ESCALATED:
            return Outcome.ESCALATED, None
        case ResolutionOutcome.FALSE_FIX:
            return Outcome.FAILED, "false_fix"
        case ResolutionOutcome.UNRESOLVED:
            return Outcome.FAILED, _unresolved_mode(resolution.run)


def failure_kind(mode: str) -> FailureKind:
    if mode == REPLAY_DIVERGENCE:
        return FailureKind.HARNESS
    return FailureKind.INFRA if mode in _INFRA_MODES else FailureKind.TASK


def _matched(task: BenchTask, outcome: Outcome, kind: FailureKind | None, correct: bool) -> bool:
    match task.expected:
        case Expectation.SOLVE:
            return outcome == Outcome.SOLVED and correct
        case Expectation.ESCALATE:
            return outcome == Outcome.ESCALATED and correct
        case Expectation.FAIL:
            return outcome == Outcome.FAILED and kind == FailureKind.TASK


def _llm_calls(run: AgentRun) -> int:
    # every step except a provider-error step answers exactly one model response
    return sum(1 for step in run.steps if step.raw_response or step.malformed or step.tool_call)


def _divergence(provider: LLMProvider) -> str | None:
    if not isinstance(provider, ReplayProvider):
        return None
    provider.check_consumed()
    return provider.divergence


def _retry_wait(provider: LLMProvider) -> float:
    if not isinstance(provider, RecordingProvider):
        return 0.0
    return sum(call.retry_wait_seconds for call in provider.calls)


async def _execute(
    task: BenchTask, provider: LLMProvider, on_step: StepCallback | None
) -> tuple[AgentRun, Outcome, str | None, bool]:
    """Run one task through today's runner; the bool is the ticket grader's verdict."""
    config = AGENT_CONFIGS[task.kind]
    if task.benchmark is not None:
        result = await run_task(task.benchmark, provider, agent_config=config, on_step=on_step)
        outcome, mode = _benchmark_outcome(result)
        return result.run, outcome, mode, True
    if task.ticket is None:
        raise ValueError(f"task {task.id!r} has no payload to run")
    resolution = await resolve_ticket(task.ticket, provider, agent_config=config, on_step=on_step)
    outcome, mode = ticket_outcome(resolution)
    return resolution.run, outcome, mode, resolution.correct


def step_event(step: AgentStep) -> dict[str, Any]:
    """A step as a small progress record: what the agent did and what it cost."""
    return {
        "step": step.index,
        "tool": step.tool_call.name.value if step.tool_call is not None else None,
        "ok": step.tool_result.ok if step.tool_result is not None else None,
        "malformed": step.malformed,
        "prompt_tokens": step.prompt_tokens,
        "completion_tokens": step.completion_tokens,
    }


class TaskExecution(BaseModel):
    """What running one task produced, whatever queue it came through."""

    outcome: Outcome
    failure_mode: str | None
    failure_kind: FailureKind | None
    matched_expectation: bool
    termination_reason: str
    iterations: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    retry_wait_seconds: float
    divergence: str | None


async def execute_task(
    task: BenchTask, provider: LLMProvider, *, on_step: StepCallback | None = None
) -> TaskExecution:
    """Run one task and classify it; every executor goes through here, so they run tasks alike."""
    run, outcome, mode, correct = await _execute(task, provider, on_step)
    divergence = _divergence(provider)
    if divergence is not None:
        outcome, mode = Outcome.FAILED, REPLAY_DIVERGENCE
    kind = failure_kind(mode) if mode is not None else None
    return TaskExecution(
        outcome=outcome,
        failure_mode=mode,
        failure_kind=kind,
        matched_expectation=_matched(task, outcome, kind, correct),
        termination_reason=run.termination_reason.value,
        iterations=run.iterations,
        llm_calls=_llm_calls(run),
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        retry_wait_seconds=_retry_wait(provider),
        divergence=divergence,
    )


def job_result(
    job: Job,
    task: BenchTask,
    execution: TaskExecution,
    *,
    worker: str,
    attempts: int,
    submitted_at: float,
    claimed_at: float,
    finished_at: float,
) -> JobResult:
    return JobResult(
        job_id=job.id,
        task_id=task.id,
        repeat=job.repeat,
        kind=task.kind.value,
        expected=task.expected.value,
        worker=worker,
        attempts=attempts,
        submitted_at=submitted_at,
        claimed_at=claimed_at,
        finished_at=finished_at,
        outcome=execution.outcome,
        failure_mode=execution.failure_mode,
        failure_kind=execution.failure_kind,
        matched_expectation=execution.matched_expectation,
        termination_reason=execution.termination_reason,
        iterations=execution.iterations,
        llm_calls=execution.llm_calls,
        prompt_tokens=execution.prompt_tokens,
        completion_tokens=execution.completion_tokens,
        retry_wait_seconds=execution.retry_wait_seconds,
        divergence=execution.divergence,
    )


class SequentialExecutor:
    """Today's implementation: one in-process worker running the jobs back to back."""

    name = "sequential"
    topology = "single host, one in-process worker"

    def __init__(self, workers: int = 1) -> None:
        if workers != 1:
            raise ValueError("the sequential executor runs exactly one worker")
        self.workers = workers

    async def run(
        self,
        taskset: TaskSet,
        jobs: Sequence[Job],
        provider_for: ProviderFactory,
        *,
        stop_check: StopCheck | None = None,
        on_result: JobCallback | None = None,
        clock: Clock = time.monotonic,
    ) -> BatchResult:
        # the whole batch is submitted at once, so every job's clock starts here
        start = clock()
        results: list[JobResult] = []
        for job in jobs:
            task = taskset.get(job.task_id)
            provider = provider_for(task)
            claimed = clock() - start
            execution = await execute_task(task, provider)
            finished = clock() - start
            reason = stop_check(provider) if stop_check is not None else None
            if reason is not None:
                return BatchResult(results, f"{reason} after {len(results)} of {len(jobs)} jobs")
            result = job_result(
                job,
                task,
                execution,
                worker="w0",
                attempts=1,
                submitted_at=0.0,
                claimed_at=claimed,
                finished_at=finished,
            )
            results.append(result)
            if on_result is not None:
                on_result(task, provider, result)
        return BatchResult(results, None)
