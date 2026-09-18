"""Shared test plumbing.

Every test in this suite runs offline. The store pages under `tests/fixtures/` are real
captures, so the parser is tested against what Cyberpuerta actually served rather than
against markup written to make the parser pass.

The database fixtures below run against in-memory SQLite. That is not the production
backend — Postgres is, from phase 6 — so anything the two disagree about is written to
behave identically at the model layer rather than left to the engine: timestamps go
through `UtcDateTime`, enums are VARCHAR with a CHECK, and foreign keys are switched on
explicitly. See `docs/database-design.md`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import StaticPool

from price_tracker.db import (
    Repository,
    create_all,
    create_engine,
    create_session_factory,
    utc_now,
)
from price_tracker.scrapers import ProductData

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    """Read a saved store page, byte-exact.

    `.gitattributes` marks `tests/fixtures/**` as `-text` so git never rewrites a line
    ending inside one; the whole value of a capture is that it has not been touched.
    """

    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh, empty database per test, living only in memory.

    `poolclass=StaticPool` is what makes this work at all. The default pool opens a new
    connection per checkout, and every connection to `:memory:` gets its *own* empty
    database — so the schema is created on one connection and the test queries another,
    which has no tables. The error that follows names a missing table and says nothing
    about pooling, which is why this line is the first thing to check when a database
    test fails inexplicably.
    """
    engine = create_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    await create_all(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the in-memory database. Nothing is committed unless a test says so."""
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session


@pytest.fixture
def repo(session: AsyncSession) -> Repository:
    return Repository(session)


@pytest.fixture
def product_data():
    """Build a `ProductData` the way the scraper would, with sane defaults.

    Defaults are the real Kingston A400 reading from `tests/fixtures/`, so a test that
    does not care about the values still exercises plausible ones.
    """

    def _build(**overrides) -> ProductData:
        fields = {
            "store": "cyberpuerta",
            "external_id": "c156664ff9062de87fc3bf694dbb8eae",
            "canonical_url": "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB.html",
            "name": "SSD Kingston A400 240GB",
            "price_cents": 88900,
            "currency": "MXN",
            "in_stock": True,
        }
        fields.update(overrides)
        return ProductData(**fields)

    return _build


@pytest.fixture
def now():
    """A fixed instant, for tests about elapsed time.

    Timestamps in this project are always timezone-aware UTC; `UtcDateTime` refuses a
    naive one, so tests cannot accidentally establish a habit the columns reject.
    """
    return utc_now()


@pytest.fixture
def hours():
    """`hours(13)` — a readable timedelta, since the alert cooldown is measured in them."""
    return lambda n: timedelta(hours=n)
