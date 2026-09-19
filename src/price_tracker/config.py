"""Application settings, loaded from the environment.

pydantic-settings is the rough equivalent of validating `process.env` with Zod: the
class below is both the schema and the parsed result, so a missing BOT_TOKEN fails at
startup with a clear message instead of surfacing as `None` somewhere deep in a handler.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
