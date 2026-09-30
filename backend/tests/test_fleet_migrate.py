import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.config import FleetSettings, async_url
from fleet.localdb import find_bin_dir
from fleet.migrate import migrate


async def test_migration_creates_the_fleet_tables_under_their_own_version_table(
    fleet_engine: AsyncEngine,
) -> None:
    async with fleet_engine.connect() as connection:
        rows = await connection.execute(
            text("select table_name from information_schema.tables where table_schema = 'public'")
        )
        tables = {row[0] for row in rows}
        version = await connection.scalar(text("select version_num from fleet_alembic_version"))
    assert {"fleet_batches", "fleet_jobs", "fleet_attempts", "fleet_results"} <= tables
    assert "alembic_version" not in tables
    assert version == "0001"


def test_migrating_again_at_head_changes_nothing(fleet_database_url: str) -> None:
    migrate(fleet_database_url)


async def _insert_job(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("insert into fleet_batches (request_digest, label) values ('d', 'b')")
        )
        await connection.execute(
            text(
                "insert into fleet_jobs (batch_id, position, name, payload, payload_digest) "
                "values (1, 0, 'j', '{}'::jsonb, 'd')"
            )
        )


async def test_a_finished_job_without_a_result_is_impossible(fleet_engine: AsyncEngine) -> None:
    await _insert_job(fleet_engine)
    with pytest.raises(IntegrityError, match="check"):
        async with fleet_engine.begin() as connection:
            await connection.execute(text("update fleet_jobs set state = 'succeeded'"))


async def test_a_claimed_job_without_a_lease_is_impossible(fleet_engine: AsyncEngine) -> None:
    await _insert_job(fleet_engine)
    with pytest.raises(IntegrityError, match="check"):
        async with fleet_engine.begin() as connection:
            await connection.execute(text("update fleet_jobs set state = 'claimed'"))


def test_plain_postgres_urls_are_pointed_at_asyncpg() -> None:
    assert async_url("postgresql://u@h/db") == "postgresql+asyncpg://u@h/db"
    assert async_url("postgres://u@h/db") == "postgresql+asyncpg://u@h/db"
    assert FleetSettings(database_url="postgres://u@h/db").database_url.startswith(
        "postgresql+asyncpg://"
    )


def test_local_postgres_binaries_are_found_when_installed() -> None:
    bin_dir = find_bin_dir()
    if bin_dir is None:
        pytest.skip("no postgres binaries on this machine")
    assert (bin_dir / "pg_ctl").exists()
