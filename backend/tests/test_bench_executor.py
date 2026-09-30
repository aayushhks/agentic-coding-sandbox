import itertools
import json

import pytest

from app.agent.types import AgentRun, TerminationReason
from app.benchmark.schema import Task, TaskCategory, TaskDifficulty, TaskMetadata
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockProvider
from app.tickets.loader import load_tickets
from app.tickets.models import ExpectedOutcome
from app.tickets.runner import ResolutionOutcome, TicketResolution
from bench.executor import SequentialExecutor, failure_kind, ticket_outcome
from bench.jobs import FailureKind, JobResult, Outcome
from bench.probes import UNSATISFIABLE_SPEC
from bench.replay import Recording, RecordingProvider, ReplayProvider, build_recording
from bench.taskset import BenchTask, Expectation, Job, TaskKind, TaskSet, plan_jobs

ADDER = Task(
    metadata=TaskMetadata(
        id="adder",
        title="Add",
        description="Implement add(a, b) in add.py returning the sum.",
        category=TaskCategory.ALGORITHMS,
        difficulty=TaskDifficulty.EASY,
    ),
    test_files={
        "test_hidden_add.py": (
            "from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        ),
    },
    reference_files={"add.py": "def add(a, b):\n    return a + b\n"},
)
TICKET = next(ticket for ticket in load_tickets() if ticket.id == "TCK-04")


def _benchmark(task: Task, expected: Expectation) -> BenchTask:
    return BenchTask(
        id=task.id,
        kind=TaskKind.BENCHMARK,
        category=task.category.value,
        difficulty=task.difficulty.value,
        expected=expected,
        benchmark=task,
    )


TASKSET = TaskSet(
    version="test",
    tasks=[
        _benchmark(ADDER, Expectation.SOLVE),
        BenchTask(
            id=TICKET.id,
            kind=TaskKind.TICKET,
            category=TICKET.category,
            difficulty="n/a",
            expected=Expectation.ESCALATE,
            ticket=TICKET,
        ),
        _benchmark(UNSATISFIABLE_SPEC, Expectation.FAIL),
    ],
)


def _call(tool: str, **arguments: object) -> str:
    return json.dumps({"thought": f"using {tool}", "tool": tool, "arguments": arguments})


SCRIPTS = {
    "adder": [
        _call("write_file", path="add.py", content="def add(a, b):\n    return a + b\n"),
        _call(
            "write_file",
            path="test_add.py",
            content="from add import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        ),
        _call("run_tests"),
        _call("finish", answer="done"),
    ],
    "TCK-04": [_call("escalate", reason="no measurable target for 'faster'")],
    "unsatisfiable_spec": [
        _call(
            "write_file",
            path="names.py",
            content="def normalize(name):\n    return name.strip().lower()\n",
        ),
        _call(
            "write_file",
            path="test_own.py",
            content=(
                "from names import normalize\n\n\n"
                "def test_it():\n    assert normalize(' A ') == 'a'\n"
            ),
        ),
        _call("run_tests"),
        _call("finish", answer="done"),
    ],
}


def _scripted(task: BenchTask) -> LLMProvider:
    return MockProvider(SCRIPTS[task.id])


async def _record_batch() -> dict[str, Recording]:
    recordings: dict[str, Recording] = {}

    def keep(task: BenchTask, provider: LLMProvider, result: JobResult) -> None:
        assert isinstance(provider, RecordingProvider)
        recordings[task.id] = build_recording(
            provider,
            task_id=task.id,
            taskset_version="test",
            outcome=result.outcome.value,
            git_sha="x",
            recorded_at="t",
        )

    await SequentialExecutor().run(
        TASKSET,
        plan_jobs(TASKSET, 3, seed=1),
        lambda task: RecordingProvider(_scripted(task)),
        on_result=keep,
    )
    return recordings


async def test_executor_runs_every_kind_and_classifies_outcomes() -> None:
    batch = await SequentialExecutor().run(TASKSET, plan_jobs(TASKSET, 3, seed=1), _scripted)
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
    batch = await SequentialExecutor().run(TASKSET, plan_jobs(TASKSET, 3, seed=1), _scripted)
    results = batch.results
    assert all(r.submitted_at == 0.0 and r.worker == "w0" and r.attempts == 1 for r in results)
    assert all(r.finished_at > r.claimed_at for r in results)
    for earlier, later in itertools.pairwise(results):
        assert later.claimed_at >= earlier.finished_at


async def test_replay_reproduces_a_recorded_batch() -> None:
    recordings = await _record_batch()
    batch = await SequentialExecutor().run(
        TASKSET, plan_jobs(TASKSET, 3, seed=1), lambda task: ReplayProvider(recordings[task.id])
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
    recording = (await _record_batch())["adder"]
    truncated = recording.model_copy(update={"calls": recording.calls[:-1]})
    batch = await SequentialExecutor().run(
        TASKSET, [Job("adder#0", "adder", 0)], lambda _task: ReplayProvider(truncated)
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
        TASKSET, plan_jobs(TASKSET, 3, seed=1), _scripted, stop_check=stop
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


def test_sequential_executor_runs_exactly_one_worker() -> None:
    with pytest.raises(ValueError, match="exactly one worker"):
        SequentialExecutor(workers=2)
