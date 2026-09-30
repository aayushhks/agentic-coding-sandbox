"""Apply the fleet's schema migrations to a Postgres database."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from fleet.config import FleetSettings, async_url

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    # the config parser treats % as interpolation, so a literal one is doubled
    config.set_main_option("sqlalchemy.url", async_url(url).replace("%", "%%"))
    return config


def migrate(url: str, revision: str = "head") -> None:
    """Upgrade the database to a revision; running it again at head changes nothing."""
    command.upgrade(alembic_config(url), revision)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="defaults to FLEET_DATABASE_URL")
    args = parser.parse_args()
    migrate(args.database_url or FleetSettings().database_url)
    print("fleet schema is at head")


if __name__ == "__main__":
    main()
