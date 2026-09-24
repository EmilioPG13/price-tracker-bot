"""Alembic environment.

Generated with `alembic init -t async` and then changed in three ways, each of which
matters:

1. **The URL comes from the application settings, not from `alembic.ini`.** The ini
   value is left blank on purpose: a connection string in a tracked file is how a
   production password ends up in a public repository. `DATABASE_URL` in the
   environment (or `.env`) is the single source, shared with the bot itself.

2. **`target_metadata` is the models' metadata**, so `--autogenerate` has something to
   compare the database against.

3. **`render_as_batch=True`.** SQLite cannot `ALTER TABLE` to drop a column, rename one,
   or add a constraint. Batch mode makes Alembic emit the copy-into-a-new-table dance
   instead. It is harmless on Postgres and it is the difference between a migration that
   runs locally and one that only runs in production.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from price_tracker.config import get_database_settings
from price_tracker.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read at runtime rather than stored in the tracked ini file (see the module docstring),
# and handed to the engine directly rather than through `config.set_main_option`. The
# config is a ConfigParser, where `%` starts an interpolation, and a password with
# symbols in it is percent-encoded in the URL. It failed exactly there on the first
# deploy, and the ConfigParser error quoted the whole URL, password included, into the
# log.
database_url = get_database_settings().database_url

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Useful for reviewing a migration."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        # Without this, a change to a column's type or nullability is not detected and
        # `--autogenerate` produces an empty migration that looks like success.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=database_url,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
