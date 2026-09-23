"""The database URL a host hands out, and the one the async engine can actually open."""

import pytest
from sqlalchemy.engine import make_url

from price_tracker.config import DatabaseSettings, async_database_url

SUPABASE_POOLER = (
    "postgresql://postgres.abcdefghijklmnopqrst:secret"
    "@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
)


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_a_url_naming_no_driver_gets_the_async_one(scheme):
    url = async_database_url(f"{scheme}://user:pw@db.example.com:5432/app")

    assert url == "postgresql+asyncpg://user:pw@db.example.com:5432/app"


def test_the_url_supabase_hands_out_is_usable_as_pasted():
    parsed = make_url(async_database_url(SUPABASE_POOLER))

    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.username == "postgres.abcdefghijklmnopqrst"
    assert parsed.host == "aws-0-us-east-1.pooler.supabase.com"
    assert parsed.port == 5432


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+asyncpg"])
def test_sslmode_becomes_the_argument_asyncpg_actually_takes(scheme):
    url = async_database_url(f"{scheme}://u:p@h/db?sslmode=require")

    assert make_url(url).drivername == "postgresql+asyncpg"
    assert make_url(url).query == {"ssl": "require"}


def test_an_explicit_ssl_setting_wins_over_sslmode():
    url = async_database_url("postgresql://u:p@h/db?sslmode=disable&ssl=require")

    assert make_url(url).query == {"ssl": "require"}


def test_a_password_that_needs_escaping_survives_the_rewrite():
    """The failure this guards is silent: a mangled password is only an auth error."""
    password = "p@ss/w:rd%?#"
    original = make_url("postgresql://user@db.example.com/app").set(password=password)

    rewritten = make_url(async_database_url(original.render_as_string(hide_password=False)))

    assert rewritten.password == password


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./price_tracker.db",
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://u:p@h/db?ssl=require",
        "postgresql+psycopg://u:p@h/db?sslmode=require",
    ],
)
def test_a_url_that_is_already_usable_is_left_exactly_alone(url):
    assert async_database_url(url) == url


def test_the_settings_apply_it_to_whatever_the_environment_says(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SUPABASE_POOLER)

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url.startswith("postgresql+asyncpg://")
