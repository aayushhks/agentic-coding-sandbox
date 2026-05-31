from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.eval.compare_cli import run_compare
from app.eval.failure import FailureMode
from app.eval.report import BenchmarkReport, TaskOutcome
from app.eval.store import load_run_rows, persist_report


def _outcome(task_id: str, solved: bool) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        category="algorithms",
        difficulty="easy",
        solved=solved,
        iterations=4,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        wall_clock_seconds=1.0,
        failure_mode=None if solved else FailureMode.WRONG_SOLUTION,
        tool_counts={"finish": 1},
        trace=[],
        eval_exit_code=0 if solved else 1,
        eval_output="",
    )


def _report(label: str, *, a_solved: bool, b_solved: bool) -> BenchmarkReport:
    return BenchmarkReport(
        label=label,
        provider="mock",
        model="mock-model",
        version="v1",
        outcomes=[_outcome("a", a_solved), _outcome("b", b_solved)],
    )


async def test_run_compare_detects_conversion_and_regression(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cmp.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await persist_report(session, _report("v1", a_solved=True, b_solved=False))
        await persist_report(session, _report("v2", a_solved=False, b_solved=True))
    await engine.dispose()

    comparison = await run_compare("v1", "v2", database_url=db_url)

    assert {d.task_id for d in comparison.regressed} == {"a"}
    assert {d.task_id for d in comparison.converted} == {"b"}
    assert comparison.has_regression


async def test_load_run_rows_missing_label_raises(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            with pytest.raises(LookupError, match="no benchmark run"):
                await load_run_rows(session, "missing")
    finally:
        await engine.dispose()
