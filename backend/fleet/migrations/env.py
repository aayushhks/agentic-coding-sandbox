"""Alembic environment for the fleet tables, kept apart from the dashboard's migrations."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# its own version table, so this history never collides with the dashboard's alembic_version
VERSION_TABLE = "fleet_alembic_version"


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None, version_table=VERSION_TABLE)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not set; run migrations through fleet.migrate")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("fleet migrations only run against a live database")
asyncio.run(_run_online())
