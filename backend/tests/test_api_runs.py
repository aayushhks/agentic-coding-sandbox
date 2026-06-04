from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_session
from app.db.base import Base
from app.eval.failure import FailureMode
from app.eval.report import BenchmarkReport, TaskOutcome
from app.eval.store import persist_report
from app.main import create_app


def _outcome(task_id: str, *, solved: bool) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        category="algorithms",
        difficulty="easy",
        solved=solved,
        iterations=4 if solved else 3,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        wall_clock_seconds=1.0,
        failure_mode=None if solved else FailureMode.WRONG_SOLUTION,
        tool_counts={"write_file": 1, "finish": 1},
        trace=[
            {"index": 0, "tool": "write_file", "thought": "write it", "observation": "ok"},
            {"index": 1, "tool": "finish", "thought": "done", "observation": ""},
        ],
        eval_exit_code=0 if solved else 1,
        eval_output="passed" if solved else "AssertionError",
    )


def _report(label: str, *, b_solved: bool) -> BenchmarkReport:
    return BenchmarkReport(
        label=label,
        provider="mock",
        model="mock-model",
        version="v1",
        outcomes=[_outcome("a", solved=True), _outcome("b", solved=b_solved)],
    )


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """An httpx client bound to the app, backed by a seeded temp SQLite database."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await persist_report(session, _report("baseline", b_solved=False))  # run 1
        await persist_report(session, _report("candidate", b_solved=True))  # run 2

    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


async def test_list_runs_returns_runs_newest_first(api: AsyncClient) -> None:
    response = await api.get("/api/runs")
    assert response.status_code == 200
    runs = response.json()
    assert [r["label"] for r in runs] == ["candidate", "baseline"]
    assert runs[0]["solve_rate"] == 1.0
    assert runs[1]["solve_rate"] == 0.5
    # the list is a summary: it must not carry the heavy per-task results
    assert "results" not in runs[0]


async def test_get_run_detail_includes_task_summaries(api: AsyncClient) -> None:
    response = await api.get("/api/runs/2")
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "candidate"
    assert {t["task_id"] for t in body["results"]} == {"a", "b"}
    # task summaries stay light — no trace in the run-detail payload
    assert "trace" not in body["results"][0]


async def test_get_run_missing_returns_404(api: AsyncClient) -> None:
    assert (await api.get("/api/runs/999")).status_code == 404


async def test_get_task_returns_full_trace(api: AsyncClient) -> None:
    response = await api.get("/api/runs/1/tasks/b")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == 1
    assert body["failure_mode"] == "wrong_solution"
    assert body["eval_output"] == "AssertionError"
    assert [step["tool"] for step in body["trace"]] == ["write_file", "finish"]


async def test_get_task_missing_returns_404(api: AsyncClient) -> None:
    assert (await api.get("/api/runs/1/tasks/nope")).status_code == 404


async def test_compare_reports_conversion(api: AsyncClient) -> None:
    response = await api.get(
        "/api/compare", params={"baseline": "baseline", "candidate": "candidate"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["converted"] == ["b"]
    assert body["regressed"] == []
    assert body["candidate_solve_rate"] > body["baseline_solve_rate"]


async def test_compare_missing_label_returns_404(api: AsyncClient) -> None:
    response = await api.get("/api/compare", params={"baseline": "baseline", "candidate": "ghost"})
    assert response.status_code == 404
