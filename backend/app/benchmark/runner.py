"""Run a single benchmark task end to end and grade it against its hidden tests."""

from dataclasses import dataclass

from app.agent.loop import Agent
from app.agent.types import AgentConfig, AgentRun
from app.benchmark.schema import Task
from app.llm.base import LLMProvider
from app.sandbox.base import Sandbox, SandboxConfig
from app.sandbox.subprocess_sandbox import SubprocessSandbox
from app.sandbox.tools import ToolCall, ToolName


@dataclass(slots=True)
class Evaluation:
    solved: bool
    exit_code: int | None
    output: str


@dataclass(slots=True)
class TaskResult:
    task_id: str
    category: str
    difficulty: str
    solved: bool
    run: AgentRun
    evaluation: Evaluation


def setup_workspace(sandbox: Sandbox, files: dict[str, str]) -> None:
    """Write a set of files into the sandbox workspace."""
    for path, content in files.items():
        sandbox.execute(ToolCall(ToolName.WRITE_FILE, {"path": path, "content": content}))


def grade(sandbox: Sandbox, task: Task) -> Evaluation:
    """Write the hidden tests into the workspace and run them; success means all pass."""
    setup_workspace(sandbox, task.test_files)
    targets = " ".join(task.test_files.keys())
    result = sandbox.execute(
        ToolCall(ToolName.RUN_COMMAND, {"command": f"python -m pytest -q {targets}"})
    )
    return Evaluation(solved=result.ok, exit_code=result.exit_code, output=result.output)


async def run_task(
    task: Task,
    provider: LLMProvider,
    *,
    agent_config: AgentConfig | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> TaskResult:
    """Set up the task workspace, let the agent solve it, then grade against hidden tests."""
    sandbox = SubprocessSandbox(sandbox_config)
    try:
        setup_workspace(sandbox, task.workspace_files)
        agent = Agent(provider, sandbox, agent_config)
        run = await agent.run(task.description)
        evaluation = grade(sandbox, task)
        return TaskResult(
            task_id=task.id,
            category=task.category.value,
            difficulty=task.difficulty.value,
            solved=evaluation.solved,
            run=run,
            evaluation=evaluation,
        )
    finally:
        sandbox.cleanup()
