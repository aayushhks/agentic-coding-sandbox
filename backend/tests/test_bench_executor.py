import itertools
from collections.abc import Sequence

import pytest

from app.agent.types import AgentRun, TerminationReason
from app.llm.base import CompletionResult, LLMProvider, Message
from app.tickets.models import ExpectedOutcome
from app.tickets.runner import ResolutionOutcome, TicketResolution
from bench.executor import SequentialExecutor, failure_kind, ticket_outcome
from bench.jobs import FailureKind, Outcome
from bench.replay import RecordingProvider, ReplayProvider, build_recording
from bench.taskset import Job, plan_jobs
from tests.bench_helpers import MINI_TASKSET, record_mini_batch, scripted_provider


async def test_executor_runs_every_kind_and_classifies_outcomes() -> None:
    batch = await SequentialExecutor().run(
        MINI_TASKSET, plan_jobs(MINI_TASKSET, 3, seed=1), scripted_provider
    )
    assert batch.interrupted is None
    by_task = {result.task_id: result for result in batch.results}
    assert (by_task["adder"].outcome, by_task["adder"].failure_mode) == (Outcome.SOLVED, None)
    assert by_task["TCK-04"].outcome == Outcome.ESCALATED
    probe = by_task["unsatisfiable_spec"]
    assert (probe.outcome, probe.failure_mode, probe.failure_kind) == (
        Outcome.FAILED,
        "wrong_solution",
        FailureKind.TASK,
    )
    assert all(result.matched_expectation for result in batch.results)
    assert {task_id: r.llm_calls for task_id, r in by_task.items()} == {
        "adder": 4,
        "TCK-04": 1,
        "unsatisfiable_spec": 4,
    }


async def test_one_worker_runs_jobs_back_to_back() -> None:
    batch = await SequentialExecutor().run(
        MINI_TASKSET, plan_jobs(MINI_TASKSET, 3, seed=1), scripted_provider
    )
    results = batch.results
    assert all(r.submitted_at == 0.0 and r.worker == "w0" and r.attempts == 1 for r in results)
    assert all(r.finished_at > r.claimed_at for r in results)
    for earlier, later in itertools.pairwise(results):
        assert later.claimed_at >= earlier.finished_at


async def test_replay_reproduces_a_recorded_batch() -> None:
    recordings = await record_mini_batch()
    batch = await SequentialExecutor().run(
        MINI_TASKSET,
        plan_jobs(MINI_TASKSET, 3, seed=1),
        lambda task: ReplayProvider(recordings[task.id]),
    )
    assert {r.task_id: r.outcome.value for r in batch.results} == {
        task_id: recording.outcome for task_id, recording in recordings.items()
    }
    assert all(r.divergence is None for r in batch.results)
    recorded_prompt_tokens = {
        task_id: sum(call.prompt_tokens for call in recording.calls)
        for task_id, recording in recordings.items()
    }
    assert {r.task_id: r.prompt_tokens for r in batch.results} == recorded_prompt_tokens


async def test_replay_divergence_is_a_harness_failure() -> None:
    recording = (await record_mini_batch())["adder"]
    truncated = recording.model_copy(update={"calls": recording.calls[:-1]})
    batch = await SequentialExecutor().run(
        MINI_TASKSET, [Job("adder#0", "adder", 0)], lambda _task: ReplayProvider(truncated)
    )
    result = batch.results[0]
    assert (result.outcome, result.failure_mode, result.failure_kind) == (
        Outcome.FAILED,
        "replay_divergence",
        FailureKind.HARNESS,
    )
    assert "only 3 were recorded" in (result.divergence or "")
    assert not result.matched_expectation


async def test_stop_check_ends_the_batch_and_drops_the_interrupted_job() -> None:
    checked: list[LLMProvider] = []

    def stop(provider: LLMProvider) -> str | None:
        checked.append(provider)
        return "daily cap reached" if len(checked) == 2 else None

    batch = await SequentialExecutor().run(
        MINI_TASKSET, plan_jobs(MINI_TASKSET, 3, seed=1), scripted_provider, stop_check=stop
    )
    assert len(batch.results) == 1
    assert batch.interrupted == "daily cap reached after 1 of 3 jobs"


@pytest.mark.parametrize(
    ("termination", "mode"),
    [
        (TerminationReason.PROVIDER_ERROR, "provider_error"),
        (TerminationReason.MALFORMED_LIMIT, "malformed_tool_call"),
        (TerminationReason.MAX_ITERATIONS, "exhausted_iterations"),
    ],
)
def test_unresolved_tickets_map_to_failure_modes(termination: TerminationReason, mode: str) -> None:
    run = AgentRun(
        steps=[],
        termination_reason=termination,
        final_answer="",
        prompt_tokens=0,
        completion_tokens=0,
    )
    resolution = TicketResolution(
        ticket_id="T",
        category="underspecified",
        expected_outcome=ExpectedOutcome.ESCALATE,
        outcome=ResolutionOutcome.UNRESOLVED,
        escalation_reason="",
        canaries_intact=True,
        run=run,
    )
    assert ticket_outcome(resolution) == (Outcome.FAILED, mode)


def test_failure_kinds_separate_agent_infra_and_harness_failures() -> None:
    assert failure_kind("wrong_solution") == FailureKind.TASK
    assert failure_kind("false_fix") == FailureKind.TASK
    assert failure_kind("provider_error") == FailureKind.INFRA
    assert failure_kind("sandbox_error") == FailureKind.INFRA
    assert failure_kind("replay_divergence") == FailureKind.HARNESS


class _RejectingProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "rejecting"

    @property
    def model(self) -> str:
        return "m"

    async def complete(
        self, messages: Sequence[Message], *, temperature: float = 0.0, max_tokens: int = 1024
    ) -> CompletionResult:
        raise RuntimeError("request too large")


async def test_a_recorded_provider_failure_replays_as_the_same_infra_failure() -> None:
    recorder = RecordingProvider(_RejectingProvider())
    job = [Job("adder#0", "adder", 0)]
    first = await SequentialExecutor().run(MINI_TASKSET, job, lambda _task: recorder)
    recording = build_recording(
        recorder,
        task_id="adder",
        taskset_version="test",
        outcome=first.results[0].outcome.value,
        git_sha="x",
        recorded_at="t",
    )
    second = await SequentialExecutor().run(
        MINI_TASKSET, job, lambda _task: ReplayProvider(recording)
    )
    for batch in (first, second):
        result = batch.results[0]
        assert (result.outcome, result.failure_mode, result.failure_kind) == (
            Outcome.FAILED,
            "provider_error",
            FailureKind.INFRA,
        )
        assert result.divergence is None


def test_sequential_executor_runs_exactly_one_worker() -> None:
    with pytest.raises(ValueError, match="exactly one worker"):
        SequentialExecutor(workers=2)
