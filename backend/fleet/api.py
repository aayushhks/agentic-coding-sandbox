"""The fleet's control plane: submit a batch, then follow its jobs and read their results."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fleet.config import FleetSettings
from fleet.models import (
    DEFAULT_MAX_ATTEMPTS,
    BatchStatus,
    JobStatus,
    NewJob,
    PublishedResult,
    Submission,
)
from fleet.policy import DEFAULT_POLICY, ExecutionPolicy, OperatorLimits, PolicyError
from fleet.store import (
    IdempotencyConflictError,
    batch_status,
    job_result,
    job_status,
    submit_batch,
)


class BatchRequest(BaseModel):
    label: str = Field(min_length=1)
    idempotency_key: str | None = None
    # how many times each job may run before infrastructure failures dead-letter it
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1)
    # limits and grants for every job in the batch; defaults deny all network access
    policy: ExecutionPolicy = DEFAULT_POLICY
    jobs: list[NewJob] = Field(min_length=1)


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


EngineDep = Annotated[AsyncEngine, Depends(_engine)]


def create_app(
    engine: AsyncEngine | None = None, *, limits: OperatorLimits | None = None
) -> FastAPI:
    """The api over a given engine, or over FLEET_DATABASE_URL when none is given."""
    ceilings = limits or FleetSettings().operator_limits()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            yield
            return
        app.state.engine = create_async_engine(FleetSettings().database_url)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="fleet", lifespan=lifespan)
    if engine is not None:
        app.state.engine = engine

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/batches", status_code=status.HTTP_201_CREATED)
    async def submit(body: BatchRequest, response: Response, db: EngineDep) -> Submission:
        """Enqueue a batch; the same key and jobs again return the original ids with a 200."""
        try:
            # the operator decides what may be granted, so an oversized ask is refused outright
            ceilings.check(body.policy)
        except PolicyError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        try:
            submission = await submit_batch(
                db,
                label=body.label,
                jobs=body.jobs,
                idempotency_key=body.idempotency_key,
                max_attempts=body.max_attempts,
                policy=body.policy,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not submission.created:
            response.status_code = status.HTTP_200_OK
        return submission

    @app.get("/batches/{batch_id}")
    async def get_batch(batch_id: int, db: EngineDep) -> BatchStatus:
        found = await batch_status(db, batch_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"batch {batch_id} not found")
        return found

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: int, db: EngineDep) -> JobStatus:
        found = await job_status(db, job_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
        return found

    @app.get("/jobs/{job_id}/result")
    async def get_result(job_id: int, db: EngineDep) -> PublishedResult:
        found = await job_result(db, job_id)
        if found is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"job {job_id} has no published result yet"
            )
        return found

    return app


app = create_app()
