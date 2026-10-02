"""Run bench jobs through today's single-process execution path."""

import difflib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import BaseModel

from app.agent.types import AgentConfig, AgentRun, AgentStep, StepCallback, TerminationReason
from app.benchmark.runner import TaskResult, run_task
from app.eval.failure import FailureMode, classify_failure
from app.llm.base import LLMProvider
from app.tickets.runner import ResolutionOutcome, TicketResolution, resolve_ticket
from bench.jobs import FailureKind, JobOutput, JobResult, Outcome
from bench.replay import RecordedCall, RecordingProvider, ReplayProvider
from bench.resources import BatchResources, DatabaseCalls
from bench.taskset import BenchTask, Expectation, Job, TaskKind, TaskSet

# the m7 settings that took the benchmark to 15/15; tickets also get the escalate tool
AGENT_CONFIGS: dict[TaskKind, AgentConfig] = {
    TaskKind.BENCHMARK: AgentConfig(require_verified_finish=True),
    TaskKind.TICKET: AgentConfig(allow_escalation=True, require_verified_finish=True),
}
# the agent's configuration for each kind of task
AgentConfigs = Mapping[TaskKind, AgentConfig]
REPLAY_DIVERGENCE = "replay_divergence"
# how much of each output a record keeps
ANSWER_CHARS = 1_000
DIFF_CHARS = 6_000
TEST_OUTPUT_CHARS = 1_500
_INFRA_MODES = {FailureMode.PROVIDER_ERROR.value, FailureMode.SANDBOX_ERROR.value}

ProviderFactory = Callable[[BenchTask], LLMProvider]
# checked after every job; a returned reason stops the batch and drops that job's result
StopCheck = Callable[[LLMProvider], str | None]
JobCallback = Callable[[BenchTask, LLMProvider, JobResult], None]
Clock = Callable[[], float]


def agent_configs(extra_rules: Sequence[str] = ()) -> dict[TaskKind, AgentConfig]:
    """The bench's agent configs, with the rules an experiment adds to every kind's prompt."""
    return {
        kind: replace(config, extra_rules=tuple(extra_rules))
        for kind, config in AGENT_CONFIGS.items()
    }


@dataclass(slots=True)
class BatchResult:
    results: list[JobResult]
    interrupted: str | None
    # the cpu the batch used and the time its workers waited on the database, when measured
    resources: BatchResources | None = None
    database: dict[str, DatabaseCalls] | None = None


class Executor(Protocol):
    name: str
    topology: str
    workers: int
    execution: dict[str, Any] | None

    async def run(
        self,
        taskset: TaskSet,
        jobs: Sequence[Job],
        provider_for: ProviderFactory,
        *,
        stop_check: StopCheck | None = None,
        on_result: JobCallback | None = None,
        clock: Clock = time.monotonic,
        configs: AgentConfigs = AGENT_CONFIGS,
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


def _answered(provider: LLMProvider) -> list[RecordedCall]:
    if isinstance(provider, RecordingProvider):
        return provider.calls
    if isinstance(provider, ReplayProvider):
        return provider.answered
    return []


def _distinct(values: Sequence[str | None]) -> list[str]:
    return sorted({value for value in values if value})


@dataclass(slots=True)
class _Ran:
    run: AgentRun
    outcome: Outcome
    mode: str | None
    # the ticket grader's verdict; a benchmark task's is in its outcome
    correct: bool
    # the task's own files, and the workspace as the agent left them
    before: dict[str, str]
    after: dict[str, str]
    tests: dict[str, Any]


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[cut: {len(text)} characters in all]"


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"[cut: {len(text)} characters in all]\n{text[-limit:]}"


def _changes(before: dict[str, str], after: dict[str, str]) -> tuple[list[str], str]:
    """The paths the agent added, changed or removed, and a unified diff of them."""
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    chunks = []
    for path in changed:
        old, new = before.get(path), after.get(path)
        lines = difflib.unified_diff(
            [] if old is None else old.splitlines(),
            [] if new is None else new.splitlines(),
            "/dev/null" if old is None else f"a/{path}",
            "/dev/null" if new is None else f"b/{path}",
            lineterm="",
        )
        chunks.append("\n".join(lines) + "\n")
    return changed, "".join(chunks)


async def _execute(
    task: BenchTask, provider: LLMProvider, configs: AgentConfigs, on_step: StepCallback | None
) -> _Ran:
    """Run one task through today's runner, keeping what it left behind and how it was graded."""
    config = configs[task.kind]
    if task.benchmark is not None:
        result = await run_task(task.benchmark, provider, agent_config=config, on_step=on_step)
        outcome, mode = _benchmark_outcome(result)
        evaluation = result.evaluation
        tests = {
            "passed": evaluation.solved,
            "exit_code": evaluation.exit_code,
            "timed_out": evaluation.timed_out,
            "output": _tail(evaluation.output, TEST_OUTPUT_CHARS),
        }
        return _Ran(
            result.run, outcome, mode, True, task.benchmark.workspace_files, result.files, tests
        )
    if task.ticket is None:
        raise ValueError(f"task {task.id!r} has no payload to run")
    resolution = await resolve_ticket(task.ticket, provider, agent_config=config, on_step=on_step)
    outcome, mode = ticket_outcome(resolution)
    hidden = resolution.hidden_tests
    # the hidden tests only run on a ticket the agent finished, so they may not have run at all
    tests = {
        "passed": None if hidden is None else hidden.ok,
        "exit_code": None if hidden is None else hidden.exit_code,
        "timed_out": None if hidden is None else hidden.timed_out,
        "output": "" if hidden is None else _tail(hidden.output, TEST_OUTPUT_CHARS),
        "canaries_intact": resolution.canaries_intact,
    }
    return _Ran(
        resolution.run,
        outcome,
        mode,
        resolution.correct,
        task.ticket.workspace_files,
        resolution.files,
        tests,
    )


def _model_seconds(provider: LLMProvider) -> float:
    if isinstance(provider, RecordingProvider):
        return sum(call.latency_seconds for call in provider.calls)
    if isinstance(provider, ReplayProvider):
        return provider.waited
    return 0.0


def _output(ran: _Ran, provider: LLMProvider) -> JobOutput:
    changed, diff = _changes(ran.before, ran.after)
    return JobOutput(
        answer=_cut(ran.run.final_answer, ANSWER_CHARS),
        escalation_reason=_cut(ran.run.escalation_reason, ANSWER_CHARS),
        files_changed=changed,
        diff=_cut(diff, DIFF_CHARS),
        tests=ran.tests,
        tools=ran.run.tool_counts(),
        model_seconds=round(_model_seconds(provider), 3),
    )


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
    served_models: list[str] = []
    fingerprints: list[str] = []
    output: JobOutput | None = None


async def execute_task(
    task: BenchTask,
    provider: LLMProvider,
    *,
    configs: AgentConfigs = AGENT_CONFIGS,
    on_step: StepCallback | None = None,
) -> TaskExecution:
    """Run one task and classify it; every executor goes through here, so they run tasks alike."""
    ran = await _execute(task, provider, configs, on_step)
    run, outcome, mode, correct = ran.run, ran.outcome, ran.mode, ran.correct
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
        served_models=_distinct([call.served_model for call in _answered(provider)]),
        fingerprints=_distinct([call.fingerprint for call in _answered(provider)]),
        output=_output(ran, provider),
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
    ran: dict[str, Any] | None = None,
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
        execution=ran,
        served_models=execution.served_models,
        fingerprints=execution.fingerprints,
        output=execution.output,
    )


class SequentialExecutor:
    """Today's implementation: one in-process worker running the jobs back to back."""

    name = "sequential"
    topology = "single host, one in-process worker"
    execution: dict[str, Any] | None = None

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
        configs: AgentConfigs = AGENT_CONFIGS,
    ) -> BatchResult:
        # the whole batch is submitted at once, so every job's clock starts here
        start = clock()
        results: list[JobResult] = []
        for job in jobs:
            task = taskset.get(job.task_id)
            provider = provider_for(task)
            claimed = clock() - start
            execution = await execute_task(task, provider, configs=configs)
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
