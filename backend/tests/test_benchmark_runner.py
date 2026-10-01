import json
import threading

import pytest

from app.benchmark import runner
from app.benchmark.loader import load_benchmark
from app.benchmark.runner import Evaluation, run_task
from app.benchmark.schema import Task
from app.llm.mock_provider import MockProvider
from app.sandbox.base import Sandbox

BENCHMARK = {task.id: task for task in load_benchmark()}


def _call(tool: str, **arguments: object) -> str:
    return json.dumps({"thought": "step", "tool": tool, "arguments": arguments})


async def test_mock_agent_solves_add_numbers() -> None:
    responses = [
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call("finish", answer="done"),
    ]
    result = await run_task(BENCHMARK["add_numbers"], MockProvider(responses=responses))
    assert result.solved
    assert result.task_id == "add_numbers"
    assert result.run.tool_counts()["write_file"] == 1


async def test_task_unsolved_when_agent_writes_nothing() -> None:
    result = await run_task(
        BENCHMARK["add_numbers"],
        MockProvider(responses=[_call("finish", answer="giving up")]),
    )
    assert not result.solved


async def test_the_hidden_tests_run_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    graded_on: list[threading.Thread] = []
    real_grade = runner.grade

    def grade(sandbox: Sandbox, task: Task) -> Evaluation:
        graded_on.append(threading.current_thread())
        return real_grade(sandbox, task)

    monkeypatch.setattr(runner, "grade", grade)
    responses = [
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call("finish", answer="done"),
    ]
    result = await run_task(BENCHMARK["add_numbers"], MockProvider(responses=responses))
    # a worker running this in process keeps heartbeating while pytest runs
    assert result.solved
    assert graded_on and graded_on[0] is not threading.main_thread()
