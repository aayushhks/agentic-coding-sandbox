"""ORM models for benchmark runs and per-task results."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class BenchmarkRun(Base):
    """One execution of the agent across the benchmark with a given provider and config."""

    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    label: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    benchmark_version: Mapped[str]
    total_tasks: Mapped[int]
    solved_count: Mapped[int]
    solve_rate: Mapped[float]
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    results: Mapped[list["TaskResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class TaskResult(Base):
    """The outcome of the agent attempting one task, including its full trace."""

    __tablename__ = "task_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("benchmark_runs.id", ondelete="CASCADE"))
    task_id: Mapped[str]
    category: Mapped[str]
    difficulty: Mapped[str]
    solved: Mapped[bool]
    iterations: Mapped[int]
    prompt_tokens: Mapped[int]
    completion_tokens: Mapped[int]
    total_tokens: Mapped[int]
    wall_clock_seconds: Mapped[float]
    failure_mode: Mapped[str | None]
    tool_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    eval_exit_code: Mapped[int | None]
    eval_output: Mapped[str] = mapped_column(Text, default="")
    run: Mapped["BenchmarkRun"] = relationship(back_populates="results")
