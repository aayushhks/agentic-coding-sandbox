import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.main import create_app
from fleet.config import async_url
from fleet.localdb import LocalPostgres, PostgresUnavailableError
from fleet.migrate import migrate


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A TestClient that runs the app's lifespan (startup and shutdown)."""
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def fleet_database_url() -> Iterator[str]:
    """A migrated Postgres for the fleet tests: TEST_DATABASE_URL, else a throwaway cluster."""
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        migrate(configured)
        yield async_url(configured)
        return
    try:
        cluster = LocalPostgres.start()
    except PostgresUnavailableError as exc:
        # ci sets REQUIRE_POSTGRES so these tests can never be skipped there by accident
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail(f"postgres is required but unavailable: {exc}")
        pytest.skip(f"no postgres for the fleet tests: {exc}")
    with cluster:
        migrate(cluster.url)
        yield cluster.url


@pytest_asyncio.fixture
async def fleet_engine(fleet_database_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on an emptied fleet schema."""
    engine = create_async_engine(fleet_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "truncate fleet_results, fleet_attempts, fleet_jobs, fleet_batches "
                "restart identity cascade"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()
