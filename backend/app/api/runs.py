"""Read-only endpoints over persisted benchmark runs."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas import (
    CompareResponse,
    RunDetail,
    RunSummary,
    TaskDetail,
)
from app.eval.compare import compare_runs
from app.eval.store import (
    load_run_detail,
    load_run_rows,
    load_runs,
    load_task_result,
)

router = APIRouter(prefix="/api", tags=["runs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(session: SessionDep) -> list[RunSummary]:
    """All benchmark runs, most recent first."""
    runs = await load_runs(session)
    return [RunSummary.model_validate(run) for run in runs]


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, session: SessionDep) -> RunDetail:
    """One run with the summary of every task it covered."""
    run = await load_run_detail(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return RunDetail.model_validate(run)


@router.get("/runs/{run_id}/tasks/{task_id}", response_model=TaskDetail)
async def get_task(run_id: int, task_id: str, session: SessionDep) -> TaskDetail:
    """One task's full record within a run, including its step-by-step trace."""
    result = await load_task_result(session, run_id, task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found in run {run_id}")
    return TaskDetail.model_validate(result)


@router.get("/compare", response_model=CompareResponse)
async def compare(
    session: SessionDep,
    baseline: Annotated[str, Query(description="label of the baseline run")],
    candidate: Annotated[str, Query(description="label of the candidate run")],
) -> CompareResponse:
    """Diff two runs by label, reusing the M7 regression comparison."""
    try:
        baseline_label, baseline_rows = await load_run_rows(session, baseline)
        candidate_label, candidate_rows = await load_run_rows(session, candidate)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        comparison = compare_runs(baseline_label, candidate_label, baseline_rows, candidate_rows)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CompareResponse.from_comparison(comparison)
