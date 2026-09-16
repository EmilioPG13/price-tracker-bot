"""Application settings, loaded from the environment.

pydantic-settings is the rough equivalent of validating `process.env` with Zod: the
class below is both the schema and the parsed result, so a missing BOT_TOKEN fails at
startup with a clear message instead of surfacing as `None` somewhere deep in a handler.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Field names map to env vars case-insensitively: bot_token <- BOT_TOKEN.
    bot_token: str
    database_url: str = "sqlite+aiosqlite:///./price_tracker.db"
    scraper_user_agent: str = "price-tracker-bot/0.1 (+https://github.com/)"
    check_interval_hours: int = 6


@lru_cache
def get_settings() -> Settings:
    """Parse the environment once and reuse the result."""
    return Settings()  # type: ignore[call-arg]  # values come from env, not from kwargs
