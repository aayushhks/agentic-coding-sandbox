"""Pydantic response models for the dashboard API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.eval.compare import RunComparison


class RunSummary(BaseModel):
    """A benchmark run without its per-task results — for the runs list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    label: str
    provider: str
    model: str
    benchmark_version: str
    total_tasks: int
    solved_count: int
    solve_rate: float
    stats: dict[str, Any]


class TaskResultSummary(BaseModel):
    """One task's outcome without the heavy step-by-step trace."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    category: str
    difficulty: str
    solved: bool
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    wall_clock_seconds: float
    failure_mode: str | None
    tool_counts: dict[str, int]


class RunDetail(RunSummary):
    """A run plus the summary of every task it covered."""

    results: list[TaskResultSummary]


class TaskDetail(TaskResultSummary):
    """A single task's full record, including its trace and evaluation output."""

    run_id: int
    trace: list[dict[str, Any]]
    eval_exit_code: int | None
    eval_output: str


class TaskDeltaOut(BaseModel):
    """One task's transition between a baseline and candidate run."""

    task_id: str
    category: str
    difficulty: str
    transition: str
    baseline_solved: bool
    candidate_solved: bool
    baseline_failure: str | None
    candidate_failure: str | None
    iteration_delta: int
    token_delta: int


class CompareResponse(BaseModel):
    """The diff between two runs, mirroring the M7 regression-comparison output."""

    baseline_label: str
    candidate_label: str
    baseline_solve_rate: float
    candidate_solve_rate: float
    solve_rate_delta: float
    token_delta: int
    converted: list[str]
    regressed: list[str]
    deltas: list[TaskDeltaOut]

    @classmethod
    def from_comparison(cls, comparison: RunComparison) -> "CompareResponse":
        return cls(
            baseline_label=comparison.baseline_label,
            candidate_label=comparison.candidate_label,
            baseline_solve_rate=comparison.baseline_solve_rate,
            candidate_solve_rate=comparison.candidate_solve_rate,
            solve_rate_delta=comparison.solve_rate_delta,
            token_delta=comparison.token_delta,
            converted=[d.task_id for d in comparison.converted],
            regressed=[d.task_id for d in comparison.regressed],
            deltas=[
                TaskDeltaOut(
                    task_id=d.task_id,
                    category=d.category,
                    difficulty=d.difficulty,
                    transition=d.transition.value,
                    baseline_solved=d.baseline_solved,
                    candidate_solved=d.candidate_solved,
                    baseline_failure=d.baseline_failure,
                    candidate_failure=d.candidate_failure,
                    iteration_delta=d.iteration_delta,
                    token_delta=d.token_delta,
                )
                for d in comparison.deltas
            ],
        )
