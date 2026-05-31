"""Persist and load benchmark runs in the database."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BenchmarkRun, TaskResult
from app.eval.compare import TaskRow
from app.eval.report import BenchmarkReport


async def persist_report(session: AsyncSession, report: BenchmarkReport) -> int:
    """Write a report and all its per-task results, returning the new run id."""
    run = BenchmarkRun(
        label=report.label,
        provider=report.provider,
        model=report.model,
        benchmark_version=report.version,
        total_tasks=report.total_tasks,
        solved_count=report.solved_count,
        solve_rate=report.solve_rate,
        stats=report.stats(),
        results=[
            TaskResult(
                task_id=outcome.task_id,
                category=outcome.category,
                difficulty=outcome.difficulty,
                solved=outcome.solved,
                iterations=outcome.iterations,
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                total_tokens=outcome.total_tokens,
                wall_clock_seconds=outcome.wall_clock_seconds,
                failure_mode=outcome.failure_mode.value if outcome.failure_mode else None,
                tool_counts=outcome.tool_counts,
                trace=outcome.trace,
                eval_exit_code=outcome.eval_exit_code,
                eval_output=outcome.eval_output,
            )
            for outcome in report.outcomes
        ],
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run.id


async def load_run_rows(session: AsyncSession, label: str) -> tuple[str, list[TaskRow]]:
    """Return the label and per-task rows of the most recent run with the given label.

    Raises LookupError if no run with that label exists.
    """
    stmt = (
        select(BenchmarkRun)
        .options(selectinload(BenchmarkRun.results))
        .where(BenchmarkRun.label == label)
        .order_by(BenchmarkRun.id.desc())
        .limit(1)
    )
    run = (await session.execute(stmt)).scalars().first()
    if run is None:
        raise LookupError(f"no benchmark run found with label {label!r}")
    rows = [
        TaskRow(
            task_id=result.task_id,
            category=result.category,
            difficulty=result.difficulty,
            solved=result.solved,
            failure_mode=result.failure_mode,
            iterations=result.iterations,
            total_tokens=result.total_tokens,
        )
        for result in run.results
    ]
    return run.label, rows
