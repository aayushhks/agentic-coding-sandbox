from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.runs import router as runs_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory

logger = get_logger(__name__)

# repo_root/frontend/dist — the built dashboard, served when it has been compiled
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class HealthResponse(BaseModel):
    status: str
    app: str
    environment: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("application starting", app=settings.app_name, environment=settings.environment)
    engine = create_engine()
    app.state.session_factory = create_session_factory(engine)
    yield
    await engine.dispose()
    logger.info("application shutdown")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            app=settings.app_name,
            environment=settings.environment,
        )

    app.include_router(runs_router)

    # Serve the compiled single-page dashboard at the root, if it has been built.
    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="dashboard")

    return app


app = create_app()
