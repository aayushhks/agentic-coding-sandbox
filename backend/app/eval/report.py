"""In-memory representation of a benchmark run and its aggregate statistics."""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.agent.types import AgentRun, AgentStep
from app.benchmark.runner import TaskResult
from app.eval.failure import FailureMode, classify_failure


def serialize_step(step: AgentStep) -> dict[str, Any]:
    tool = step.tool_call
    result = step.tool_result
    return {
        "index": step.index,
        "thought": step.thought,
        "raw_response": step.raw_response,
        "tool": tool.name.value if tool else None,
        "arguments": tool.arguments if tool else None,
        "observation": step.observation,
        "malformed": step.malformed,
        "prompt_tokens": step.prompt_tokens,
        "completion_tokens": step.completion_tokens,
        "tool_ok": result.ok if result else None,
        "exit_code": result.exit_code if result else None,
        "timed_out": result.timed_out if result else None,
    }


def serialize_run(run: AgentRun) -> list[dict[str, Any]]:
    return [serialize_step(step) for step in run.steps]


@dataclass(slots=True)
class TaskOutcome:
    task_id: str
    category: str
    difficulty: str
    solved: bool
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    wall_clock_seconds: float
    failure_mode: FailureMode | None
    tool_counts: dict[str, int]
    trace: list[dict[str, Any]]
    eval_exit_code: int | None
    eval_output: str

    @classmethod
    def from_result(cls, result: TaskResult, wall_clock_seconds: float) -> "TaskOutcome":
        run = result.run
        return cls(
            task_id=result.task_id,
            category=result.category,
            difficulty=result.difficulty,
            solved=result.solved,
            iterations=run.iterations,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            total_tokens=run.total_tokens,
            wall_clock_seconds=wall_clock_seconds,
            failure_mode=classify_failure(result),
            tool_counts=run.tool_counts(),
            trace=serialize_run(run),
            eval_exit_code=result.evaluation.exit_code,
            eval_output=result.evaluation.output,
        )


@dataclass(slots=True)
class BenchmarkReport:
    label: str
    provider: str
    model: str
    version: str
    outcomes: list[TaskOutcome]

    @property
    def total_tasks(self) -> int:
        return len(self.outcomes)

    @property
    def solved_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.solved)

    @property
    def solve_rate(self) -> float:
        return self.solved_count / self.total_tasks if self.outcomes else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(outcome.total_tokens for outcome in self.outcomes)

    @property
    def average_iterations(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(outcome.iterations for outcome in self.outcomes) / self.total_tasks

    def solve_rate_by_category(self) -> dict[str, float]:
        totals: Counter[str] = Counter()
        solved: Counter[str] = Counter()
        for outcome in self.outcomes:
            totals[outcome.category] += 1
            if outcome.solved:
                solved[outcome.category] += 1
        return {category: solved[category] / totals[category] for category in totals}

    def failure_taxonomy(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for outcome in self.outcomes:
            if outcome.failure_mode is not None:
                counts[outcome.failure_mode.value] += 1
        return dict(counts)

    def stats(self) -> dict[str, Any]:
        return {
            "solve_rate": self.solve_rate,
            "average_iterations": self.average_iterations,
            "total_tokens": self.total_tokens,
            "solve_rate_by_category": self.solve_rate_by_category(),
            "failure_taxonomy": self.failure_taxonomy(),
        }
