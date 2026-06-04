from app.core.config import Settings, normalize_database_url


def test_normalize_coerces_sync_postgres_urls() -> None:
    assert normalize_database_url("postgres://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    assert (
        normalize_database_url("postgresql://u:p@h:5432/d") == "postgresql+asyncpg://u:p@h:5432/d"
    )


def test_normalize_leaves_async_and_sqlite_urls_untouched() -> None:
    assert normalize_database_url("postgresql+asyncpg://u@h/d") == "postgresql+asyncpg://u@h/d"
    assert normalize_database_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


def test_settings_coerces_url_and_parses_cors_origins() -> None:
    settings = Settings(
        database_url="postgres://u:p@h:5432/d",
        cors_origins="https://a.app, https://b.app",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/d"
    assert settings.cors_origin_list == ["https://a.app", "https://b.app"]
