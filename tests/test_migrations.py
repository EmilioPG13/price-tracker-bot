"""One test, guarding the failure mode Alembic is famous for.

A model changes, the migration does not, and nothing notices until a deploy hits a
production database that has a column the code does not expect — or lacks one it does.
The local database is usually fine, because tests build it with `create_all` straight
from the models and never run a migration at all.

So this test runs the migrations for real, against an empty database, and asks Alembic
whether the result still differs from the models. The answer must be no.

It runs on whichever backend the suite is on, and the Postgres run is the one that
matters: the migration was generated on SQLite, and the column types, the CHECK and the
enum's `server_default` are exactly where the two backends read the same DDL differently.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from price_tracker.config import get_database_settings
from price_tracker.db import Base, create_engine

ROOT = Path(__file__).resolve().parents[1]


def _on_connection[T](url: str, work: Callable[[sa.Connection], T]) -> T:
    """Run synchronous inspection code on a connection from the async engine.

    Alembic's comparison API and `sa.inspect` are synchronous. The project installs only
    async drivers, so there is no sync engine to open on Postgres; `run_sync` lends the
    async connection's sync face to `work` instead. `asyncio.run` is safe here because
    these tests are synchronous — as is `command.upgrade`, which calls it too.
    """

    async def go() -> T:
        engine = create_engine(url)
        try:
            async with engine.begin() as connection:
                return await connection.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _drop_everything(connection: sa.Connection) -> None:
    Base.metadata.drop_all(connection)
    connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def migrated_database(tmp_path, monkeypatch, postgres_url):
    """An empty database with `alembic upgrade head` applied to it; yields its URL.

    The URL is injected through the environment because that is exactly how `env.py`
    finds it in production — pointing Alembic somewhere else here would test a
    configuration path nobody uses.

    On SQLite the database is a fresh file. On Postgres it is the shared test database,
    so it is emptied first, including `alembic_version`: with that table left behind,
    Alembic would believe the schema is already at head and run nothing.
    """
    if postgres_url is None:
        url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
    else:
        url = postgres_url
        _on_connection(url, _drop_everything)

    monkeypatch.setenv("DATABASE_URL", url)
    get_database_settings.cache_clear()

    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "alembic"))

    try:
        command.upgrade(config, "head")
        yield url
    finally:
        get_database_settings.cache_clear()


def test_the_migrations_build_exactly_what_the_models_describe(migrated_database):
    def differences(connection: sa.Connection) -> list:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return compare_metadata(context, Base.metadata)

    assert _on_connection(migrated_database, differences) == [], (
        "the migrations and the models have drifted — run "
        "`uv run alembic revision --autogenerate -m '...'`"
    )


def test_the_migrations_can_be_reversed(migrated_database):
    """A downgrade that does not work is a migration that cannot be rolled back.

    Worth one test because the generated `downgrade()` is the half nobody reads, and
    the moment it is needed is the worst moment to find out it was wrong.
    """
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "alembic"))

    command.downgrade(config, "base")

    def table_names(connection: sa.Connection) -> set[str]:
        return set(sa.inspect(connection).get_table_names())

    assert _on_connection(migrated_database, table_names) == {"alembic_version"}
