import json

import pytest

from app.agent.types import AgentConfig
from app.benchmark.loader import load_benchmark
from app.benchmark.schema import Task
from app.eval.harness import run_benchmark
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockProvider

TASKS = {task.id: task for task in load_benchmark()}


def _call(tool: str, **arguments: object) -> str:
    return json.dumps({"thought": "step", "tool": tool, "arguments": arguments})


SCRIPTS = {
    "add_numbers": [
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call("finish", answer="done"),
    ],
    "two_sum": [
        _call(
            "write_file",
            path="solution.py",
            content="def two_sum(nums, target):\n    return (0, 0)\n",
        ),
        _call("finish", answer="done"),
    ],
    "reverse_string": ["garbage one", "garbage two", "garbage three"],
}


def _factory(task: Task) -> LLMProvider:
    return MockProvider(SCRIPTS[task.id])


async def test_run_benchmark_aggregates_and_classifies_failures() -> None:
    tasks = [TASKS[task_id] for task_id in ("add_numbers", "two_sum", "reverse_string")]
    report = await run_benchmark(
        tasks,
        _factory,
        label="mock-smoke",
        provider_name="mock",
        model="mock-model",
        agent_config=AgentConfig(max_iterations=5, max_consecutive_malformed=3),
    )

    assert report.total_tasks == 3
    assert report.solved_count == 1
    assert report.solve_rate == pytest.approx(1 / 3)

    taxonomy = report.failure_taxonomy()
    assert taxonomy.get("wrong_solution") == 1
    assert taxonomy.get("malformed_tool_call") == 1

    solved = next(outcome for outcome in report.outcomes if outcome.task_id == "add_numbers")
    assert solved.solved
    assert solved.tool_counts.get("write_file") == 1
    assert solved.trace

    stats = report.stats()
    assert "solve_rate_by_category" in stats
    assert stats["solve_rate"] == pytest.approx(1 / 3)
