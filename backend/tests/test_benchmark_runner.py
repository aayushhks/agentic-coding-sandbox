import json

from app.benchmark.loader import load_benchmark
from app.benchmark.runner import run_task
from app.llm.mock_provider import MockProvider

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
