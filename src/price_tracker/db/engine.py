"""Building the async engine, and the one pragma without which SQLite lies.

The application makes one engine at startup and hands out sessions from it. The tests
make a throwaway in-memory one per test. Both go through `create_engine` here so the
SQLite configuration cannot drift between them — which matters, because both of the
settings below are invisible when they are wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from price_tracker.db.models import Base


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_engine(url: str, *, echo: bool = False, **kwargs: object) -> AsyncEngine:
    """Build the async engine for `url` and enforce foreign keys on SQLite.

    **SQLite does not enforce foreign keys unless asked, on every connection.** The
    default is off, for backwards compatibility with SQLite 3.6. That means every
    `ON DELETE CASCADE` in `models.py` is silently a no-op: deleting a user leaves their
    trackings behind as orphans pointing at a row that no longer exists, and nothing
    raises. Postgres enforces them, so this is another bug that exists only where it is
    hardest to see — in the tests passing while production behaves differently, or the
    reverse. The listener below turns it on for each new connection.
    """
    engine = create_async_engine(url, echo=echo, **kwargs)

    if _is_sqlite(url):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions that survive a commit.

    `expire_on_commit=False` because the default expires every loaded attribute after a
    commit, and reading one back then emits a lazy SELECT — which an async session
    cannot do outside a greenlet context. The symptom is a `MissingGreenlet` on an
    innocent-looking `product.name` right after saving it.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception.

    Both runtimes use this. The `/add` handler wraps one command in it; the phase 4
    checker wraps each product separately, so one store failing does not discard the
    prices already read from the others.
    """
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def create_all(engine: AsyncEngine) -> None:
    """Create every table from the models, skipping Alembic.

    For tests and for a throwaway local database only. Production schema changes go
    through `alembic upgrade head`, because that is the only path that can also *alter*
    a table that already holds rows.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
