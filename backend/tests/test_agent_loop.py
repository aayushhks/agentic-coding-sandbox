import json
from collections.abc import Iterator

import pytest

from app.agent.loop import Agent
from app.agent.types import AgentConfig, TerminationReason
from app.llm.mock_provider import MockProvider
from app.sandbox.base import SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox
from app.sandbox.tools import ToolName


@pytest.fixture
def sandbox() -> Iterator[SubprocessSandbox]:
    sb = SubprocessSandbox(SandboxConfig(timeout_seconds=10.0))
    try:
        yield sb
    finally:
        sb.cleanup()


def _call(tool: str, **arguments: object) -> str:
    return json.dumps({"thought": f"using {tool}", "tool": tool, "arguments": arguments})


async def test_agent_solves_a_task_with_the_real_sandbox(sandbox: SubprocessSandbox) -> None:
    responses = [
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call(
            "write_file",
            path="test_solution.py",
            content="from solution import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        ),
        _call("run_tests"),
        _call("finish", answer="implemented add"),
    ]
    agent = Agent(MockProvider(responses=responses), sandbox, AgentConfig(max_iterations=10))

    run = await agent.run("Implement add(a, b) in solution.py with a passing test.")

    assert run.termination_reason == TerminationReason.FINISHED
    assert run.final_answer == "implemented add"
    assert run.iterations == 4
    assert run.tool_counts() == {"write_file": 2, "run_tests": 1, "finish": 1}
    assert run.total_tokens > 0

    test_step = next(s for s in run.steps if s.tool_call and s.tool_call.name == ToolName.RUN_TESTS)
    assert test_step.tool_result is not None
    assert test_step.tool_result.ok
    assert (sandbox.workspace / "solution.py").is_file()


async def test_agent_stops_at_max_iterations(sandbox: SubprocessSandbox) -> None:
    responses = [_call("list_dir", path=".") for _ in range(3)]
    agent = Agent(MockProvider(responses=responses), sandbox, AgentConfig(max_iterations=3))
    run = await agent.run("loop without finishing")
    assert run.termination_reason == TerminationReason.MAX_ITERATIONS
    assert run.iterations == 3


async def test_agent_gives_up_after_repeated_malformed(sandbox: SubprocessSandbox) -> None:
    agent = Agent(
        MockProvider(responses=["not json", "still not json", "nope"]),
        sandbox,
        AgentConfig(max_iterations=10, max_consecutive_malformed=3),
    )
    run = await agent.run("respond badly")
    assert run.termination_reason == TerminationReason.MALFORMED_LIMIT
    assert run.iterations == 3
    assert all(step.malformed for step in run.steps)


async def test_agent_recovers_after_one_malformed(sandbox: SubprocessSandbox) -> None:
    responses = ["garbage", _call("finish", answer="ok")]
    agent = Agent(MockProvider(responses=responses), sandbox, AgentConfig(max_iterations=10))
    run = await agent.run("recover from a bad response")
    assert run.termination_reason == TerminationReason.FINISHED
    assert run.steps[0].malformed
    assert not run.steps[1].malformed


async def test_agent_records_provider_error(sandbox: SubprocessSandbox) -> None:
    agent = Agent(
        MockProvider(responses=[_call("list_dir", path=".")]),
        sandbox,
        AgentConfig(max_iterations=10),
    )
    run = await agent.run("needs more steps than the provider can supply")
    assert run.termination_reason == TerminationReason.PROVIDER_ERROR


async def test_finish_blocked_until_tests_pass(sandbox: SubprocessSandbox) -> None:
    responses = [
        _call("finish", answer="too early"),  # rejected: nothing has been verified yet
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call(
            "write_file",
            path="test_solution.py",
            content="from solution import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        ),
        _call("run_tests"),  # passes -> now verified
        _call("finish", answer="done"),  # honored
    ]
    agent = Agent(
        MockProvider(responses=responses),
        sandbox,
        AgentConfig(max_iterations=10, require_verified_finish=True),
    )
    run = await agent.run("implement add with a passing test")

    assert run.termination_reason == TerminationReason.FINISHED
    assert run.final_answer == "done"
    assert run.iterations == 5
    first = run.steps[0]
    assert first.tool_call is not None and first.tool_call.name == ToolName.FINISH
    assert first.tool_result is None
    assert "cannot finish yet" in first.observation


async def test_no_tests_ran_does_not_verify(sandbox: SubprocessSandbox) -> None:
    responses = [
        _call("write_file", path="solution.py", content="def add(a, b):\n    return a + b\n"),
        _call("run_tests"),  # no test file present -> "no tests ran" (exit 5), not verification
        _call("finish", answer="done"),  # rejected, so the loop runs out of iterations
    ]
    agent = Agent(
        MockProvider(responses=responses),
        sandbox,
        AgentConfig(max_iterations=3, require_verified_finish=True),
    )
    run = await agent.run("an empty suite must not count as verified")

    assert run.termination_reason == TerminationReason.MAX_ITERATIONS
    test_step = next(s for s in run.steps if s.tool_call and s.tool_call.name == ToolName.RUN_TESTS)
    assert test_step.tool_result is not None
    assert test_step.tool_result.exit_code == 5
    assert "cannot finish yet" in run.steps[-1].observation


async def test_agent_escalates_when_allowed(sandbox: SubprocessSandbox) -> None:
    responses = [_call("escalate", reason="ticket is underspecified")]
    agent = Agent(
        MockProvider(responses=responses),
        sandbox,
        AgentConfig(max_iterations=10, allow_escalation=True),
    )
    run = await agent.run("do something impossibly vague")
    assert run.termination_reason == TerminationReason.ESCALATED
    assert run.escalated
    assert run.escalation_reason == "ticket is underspecified"
    assert run.iterations == 1
