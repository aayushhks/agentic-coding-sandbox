"""Run the agent across a set of benchmark tasks and collect a report."""

import time
from collections.abc import Callable

from app.agent.types import AgentConfig
from app.benchmark.runner import run_task
from app.benchmark.schema import Task
from app.eval.report import BenchmarkReport, TaskOutcome
from app.llm.base import LLMProvider
from app.sandbox.base import SandboxConfig

ProviderFactory = Callable[[Task], LLMProvider]


async def run_benchmark(
    tasks: list[Task],
    provider_factory: ProviderFactory,
    *,
    label: str,
    provider_name: str,
    model: str,
    version: str = "v1",
    agent_config: AgentConfig | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> BenchmarkReport:
    """Solve every task in order and return a report with per-task outcomes and aggregates.

    ``provider_factory`` returns the provider to use for a given task. For a real run this is
    a single shared provider (``lambda _task: groq``); tests use it to hand each task its own
    scripted mock.
    """
    outcomes: list[TaskOutcome] = []
    for task in tasks:
        provider = provider_factory(task)
        start = time.monotonic()
        result = await run_task(
            task, provider, agent_config=agent_config, sandbox_config=sandbox_config
        )
        elapsed = time.monotonic() - start
        outcomes.append(TaskOutcome.from_result(result, elapsed))
    return BenchmarkReport(
        label=label,
        provider=provider_name,
        model=model,
        version=version,
        outcomes=outcomes,
    )
