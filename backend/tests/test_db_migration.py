import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def test_migration_creates_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    command.upgrade(Config(str(ALEMBIC_INI)), "head")

    connection = sqlite3.connect(db)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in rows}
    finally:
        connection.close()

    assert {"benchmark_runs", "task_results", "alembic_version"} <= tables
