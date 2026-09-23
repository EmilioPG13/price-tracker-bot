"""Application settings, loaded from the environment.

pydantic-settings is the rough equivalent of validating `process.env` with Zod: the
class below is both the schema and the parsed result, so a missing BOT_TOKEN fails at
startup with a clear message instead of surfacing as `None` somewhere deep in a handler.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def async_database_url(url: str) -> str:
    """Turn a Postgres URL as a host hands it out into one the async engine can open.

    Supabase's dashboard, like every Postgres host, gives a libpq-style URL:
    `postgresql://…`, sometimes with `?sslmode=require`. Pasted as-is, each half fails
    somewhere that does not point back at the URL:

    - A scheme with no driver means SQLAlchemy's default, psycopg2 — synchronous and not
      installed — so it fails as `ModuleNotFoundError: No module named 'psycopg2'`. And
      `postgres://` is not a scheme SQLAlchemy accepts at all any more.
    - SQLAlchemy passes the query string straight to `asyncpg.connect()` as keyword
      arguments, and `sslmode` is not one: a `TypeError` from inside the first
      connection. asyncpg calls the same setting `ssl`, with the same values.

    Neither rewrite is a guess. The engine is async, so a URL naming no driver can only
    mean asyncpg; and `sslmode` and `ssl` take the same vocabulary. A URL naming some
    other driver is left alone, and so is anything already correct.
    """
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+asyncpg")
    if parsed.drivername == "postgresql+asyncpg" and "sslmode" in parsed.query:
        query = dict(parsed.query)
        query.setdefault("ssl", query.pop("sslmode"))
        parsed = parsed.set(query=query)

    if parsed == make_url(url):
        return url
    return parsed.render_as_string(hide_password=False)


class DatabaseSettings(BaseSettings):
    """Just enough configuration to open the database.

    Split out from `Settings` for Alembic. A migration has nothing to do with Telegram,
    and running one in CI or from a shell where `BOT_TOKEN` is unset should not fail on
    a missing field it will never read. Keeping the default here rather than duplicating
    it means the bot and the migrations cannot disagree about where the database is.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./price_tracker.db"

    @field_validator("database_url")
    @classmethod
    def _use_an_async_driver(cls, value: str) -> str:
        return async_database_url(value)


class Settings(DatabaseSettings):
    # Field names map to env vars case-insensitively: bot_token <- BOT_TOKEN.
    bot_token: str
    scraper_user_agent: str = "price-tracker-bot/0.1 (+https://github.com/)"

    #: How stale a product has to be before the checker spends a request on it, and how
    #: often the job runs. One number for both on purpose: they are the same question
    #: asked from two ends, and letting them differ makes a run that fetches nothing.
    check_interval_hours: int = 6

    #: The most products one pass will read. Requests to a store are spaced out, so a
    #: long due list is minutes of deliberate waiting; stopping early leaves the oldest
    #: handled and the rest first in line next time. Raise it when the catalogue grows
    #: past what one pass can cover in an interval.
    check_batch_limit: int = 25


@lru_cache
def get_settings() -> Settings:
    """Parse the environment once and reuse the result."""
    return Settings()  # type: ignore[call-arg]  # values come from env, not from kwargs


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """The database URL alone, for Alembic and anything else that needs no bot token."""
    return DatabaseSettings()
