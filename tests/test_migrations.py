"""One test, guarding the failure mode Alembic is famous for.

A model changes, the migration does not, and nothing notices until a deploy hits a
production database that has a column the code does not expect — or lacks one it does.
The local database is usually fine, because tests build it with `create_all` straight
from the models and never run a migration at all.

So this test runs the migrations for real, against an empty file, and asks Alembic
whether the result still differs from the models. The answer must be no.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from price_tracker.config import get_database_settings
from price_tracker.db import Base

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated_database(tmp_path, monkeypatch):
    """An empty database with `alembic upgrade head` applied to it.

    The URL is injected through the environment because that is exactly how `env.py`
    finds it in production — pointing Alembic somewhere else here would test a
    configuration path nobody uses.
    """
    database = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_database_settings.cache_clear()

    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "alembic"))

    try:
        command.upgrade(config, "head")
        yield database
    finally:
        get_database_settings.cache_clear()


def test_the_migrations_build_exactly_what_the_models_describe(migrated_database):
    engine = sa.create_engine(f"sqlite:///{migrated_database}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == [], (
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

    engine = sa.create_engine(f"sqlite:///{migrated_database}")
    try:
        with engine.connect() as connection:
            remaining = set(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()

    assert remaining == {"alembic_version"}
