from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import BenchmarkRun
from app.db.session import create_session_factory
from app.eval.failure import FailureMode
from app.eval.report import BenchmarkReport, TaskOutcome
from app.eval.store import persist_report


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def _report() -> BenchmarkReport:
    outcomes = [
        TaskOutcome(
            task_id="add_numbers",
            category="algorithms",
            difficulty="easy",
            solved=True,
            iterations=2,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            wall_clock_seconds=0.5,
            failure_mode=None,
            tool_counts={"write_file": 1, "finish": 1},
            trace=[{"index": 0, "tool": "write_file"}],
            eval_exit_code=0,
            eval_output="passed",
        ),
        TaskOutcome(
            task_id="two_sum",
            category="algorithms",
            difficulty="medium",
            solved=False,
            iterations=3,
            prompt_tokens=20,
            completion_tokens=8,
            total_tokens=28,
            wall_clock_seconds=1.0,
            failure_mode=FailureMode.WRONG_SOLUTION,
            tool_counts={"write_file": 1, "finish": 1},
            trace=[{"index": 0, "tool": "write_file"}],
            eval_exit_code=1,
            eval_output="failed",
        ),
    ]
    return BenchmarkReport(
        label="test-run", provider="mock", model="mock-model", version="v1", outcomes=outcomes
    )


async def test_persist_and_read_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        run_id = await persist_report(session, _report())
    assert run_id >= 1

    async with session_factory() as session:
        stmt = (
            select(BenchmarkRun)
            .options(selectinload(BenchmarkRun.results))
            .where(BenchmarkRun.id == run_id)
        )
        run = (await session.execute(stmt)).scalar_one()
        assert run.label == "test-run"
        assert run.total_tasks == 2
        assert run.solved_count == 1
        assert run.solve_rate == 0.5
        assert run.stats["failure_taxonomy"] == {"wrong_solution": 1}

        assert len(run.results) == 2
        by_id = {result.task_id: result for result in run.results}
        assert by_id["add_numbers"].solved is True
        assert by_id["two_sum"].failure_mode == "wrong_solution"
        assert isinstance(by_id["add_numbers"].trace, list)
        assert by_id["add_numbers"].trace[0]["tool"] == "write_file"
