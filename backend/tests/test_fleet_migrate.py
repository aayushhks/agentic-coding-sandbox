import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from fleet.config import FleetSettings, async_url
from fleet.localdb import find_bin_dir
from fleet.migrate import alembic_config, migrate


async def _version(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        version = await connection.scalar(text("select version_num from fleet_alembic_version"))
        return None if version is None else str(version)


async def test_migration_creates_the_fleet_tables_under_their_own_version_table(
    fleet_engine: AsyncEngine,
) -> None:
    async with fleet_engine.connect() as connection:
        rows = await connection.execute(
            text("select table_name from information_schema.tables where table_schema = 'public'")
        )
        tables = {row[0] for row in rows}
    assert {"fleet_batches", "fleet_jobs", "fleet_attempts", "fleet_results"} <= tables
    assert "alembic_version" not in tables
    assert await _version(fleet_engine) == "0003"


def test_migrating_again_at_head_changes_nothing(fleet_database_url: str) -> None:
    migrate(fleet_database_url)


async def test_the_later_migrations_downgrade_and_upgrade_again(
    fleet_engine: AsyncEngine, fleet_database_url: str
) -> None:
    # alembic runs its own event loop, so it gets a thread of its own
    await asyncio.to_thread(command.downgrade, alembic_config(fleet_database_url), "0001")
    assert await _version(fleet_engine) == "0001"
    await asyncio.to_thread(migrate, fleet_database_url)
    assert await _version(fleet_engine) == "0003"


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


async def test_a_dead_letter_is_final_without_a_result_or_a_lease(
    fleet_engine: AsyncEngine,
) -> None:
    await _insert_job(fleet_engine)
    async with fleet_engine.begin() as connection:
        await connection.execute(text("update fleet_jobs set state = 'dead_lettered'"))
    with pytest.raises(IntegrityError, match="check"):
        async with fleet_engine.begin() as connection:
            await connection.execute(
                text("update fleet_jobs set lease_expires_at = now() + interval '1 minute'")
            )


async def test_attempts_past_the_retry_budget_are_impossible(fleet_engine: AsyncEngine) -> None:
    await _insert_job(fleet_engine)
    with pytest.raises(IntegrityError, match="fleet_jobs_attempts_within_budget"):
        async with fleet_engine.begin() as connection:
            await connection.execute(text("update fleet_jobs set max_attempts = 2, attempt = 3"))


async def test_an_attempt_that_ended_must_say_how(fleet_engine: AsyncEngine) -> None:
    await _insert_job(fleet_engine)
    with pytest.raises(IntegrityError, match="fleet_attempts_ended_with_reason"):
        async with fleet_engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into fleet_attempts "
                    "(job_id, attempt, worker_id, claimed_at, lease_expires_at, ended_at) "
                    "values (1, 1, 'w', now(), now(), now())"
                )
            )


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
