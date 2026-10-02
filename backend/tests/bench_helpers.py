"""Builders shared by the bench tests."""

import json
from collections.abc import Sequence

from app.benchmark.schema import Task, TaskCategory, TaskDifficulty, TaskMetadata
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockProvider
from app.tickets.loader import load_tickets
from bench.environment import Environment
from bench.executor import SequentialExecutor, agent_configs
from bench.jobs import JobResult, Outcome
from bench.metrics import PERCENTILE_METHOD, compute_metrics
from bench.probes import UNSATISFIABLE_SPEC
from bench.records import BenchConfig, TrialRecord
from bench.replay import Recording, RecordingProvider, build_recording
from bench.taskset import BenchTask, Expectation, TaskKind, TaskSet, plan_jobs


def make_config(**overrides: object) -> BenchConfig:
    fields: dict[str, object] = {
        "mode": "replay",
        "latency": "zero",
        "executor": "sequential",
        "workers": 1,
        "topology": "single host, one in-process worker",
        "tasks": 2,
        "seed": 1,
        "taskset_version": "v1",
        "taskset_digest": "t" * 64,
        "recordings_digest": "r" * 64,
        "provider": "replay",
        "model": "qwen/qwen3.8-27b",
        "agent_configs": {"benchmark": {"max_iterations": 15}},
        "sandbox_config": {"timeout_seconds": 10.0},
        "tool_transport": "in_process",
        "percentile_method": PERCENTILE_METHOD,
    }
    fields.update(overrides)
    return BenchConfig.model_validate(fields)


def make_environment() -> Environment:
    return Environment(
        git_sha="a" * 40,
        git_dirty=False,
        cpu_model="Test CPU",
        logical_cpus=4,
        usable_cpus=4,
        cpu_quota_cores=None,
        memory_total_gib=16.0,
        memory_limit_gib=None,
        virtualized=True,
        os="Test OS",
        kernel="6.0",
        python="3.13.0",
    )


def make_trial(trial: int, jobs: list[JobResult], **overrides: object) -> TrialRecord:
    fields: dict[str, object] = {
        "label": "demo",
        "trial": trial,
        "started_at": "2026-09-30T00:00:00+00:00",
        "config": make_config(),
        "environment": make_environment(),
        "metrics": compute_metrics(jobs, workers=1),
        "jobs": jobs,
    }
    fields.update(overrides)
    return TrialRecord.model_validate(fields)


def make_job(claimed: float, finished: float, **overrides: object) -> JobResult:
    fields: dict[str, object] = {
        "job_id": f"t{claimed}#0",
        "task_id": f"t{claimed}",
        "repeat": 0,
        "kind": "benchmark",
        "expected": "solve",
        "worker": "w0",
        "attempts": 1,
        "submitted_at": 0.0,
        "claimed_at": claimed,
        "finished_at": finished,
        "outcome": Outcome.SOLVED,
        "failure_mode": None,
        "failure_kind": None,
        "matched_expectation": True,
        "termination_reason": "finished",
        "iterations": 4,
        "llm_calls": 4,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "retry_wait_seconds": 0.0,
        "divergence": None,
    }
    fields.update(overrides)
    return JobResult.model_validate(fields)


# a three-task set covering a solve, an escalation and an expected failure
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


def _benchmark_task(task: Task, expected: Expectation) -> BenchTask:
    return BenchTask(
        id=task.id,
        kind=TaskKind.BENCHMARK,
        category=task.category.value,
        difficulty=task.difficulty.value,
        expected=expected,
        benchmark=task,
    )


MINI_TASKSET = TaskSet(
    version="test",
    tasks=[
        _benchmark_task(ADDER, Expectation.SOLVE),
        BenchTask(
            id=TICKET.id,
            kind=TaskKind.TICKET,
            category=TICKET.category,
            difficulty="n/a",
            expected=Expectation.ESCALATE,
            ticket=TICKET,
        ),
        _benchmark_task(UNSATISFIABLE_SPEC, Expectation.FAIL),
    ],
)


def tool_call(tool: str, **arguments: object) -> str:
    return json.dumps({"thought": f"using {tool}", "tool": tool, "arguments": arguments})


SCRIPTS = {
    "adder": [
        tool_call("write_file", path="add.py", content="def add(a, b):\n    return a + b\n"),
        tool_call(
            "write_file",
            path="test_add.py",
            content="from add import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        ),
        tool_call("run_tests"),
        tool_call("finish", answer="done"),
    ],
    "TCK-04": [tool_call("escalate", reason="no measurable target for 'faster'")],
    "unsatisfiable_spec": [
        tool_call(
            "write_file",
            path="names.py",
            content="def normalize(name):\n    return name.strip().lower()\n",
        ),
        tool_call(
            "write_file",
            path="test_own.py",
            content=(
                "from names import normalize\n\n\n"
                "def test_it():\n    assert normalize(' A ') == 'a'\n"
            ),
        ),
        tool_call("run_tests"),
        tool_call("finish", answer="done"),
    ],
}


def scripted_provider(task: BenchTask) -> LLMProvider:
    return MockProvider(SCRIPTS[task.id])


async def record_mini_batch(extra_rules: Sequence[str] = ()) -> dict[str, Recording]:
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
        MINI_TASKSET,
        plan_jobs(MINI_TASKSET, 3, seed=1),
        lambda task: RecordingProvider(scripted_provider(task)),
        on_result=keep,
        configs=agent_configs(extra_rules),
    )
    return recordings
