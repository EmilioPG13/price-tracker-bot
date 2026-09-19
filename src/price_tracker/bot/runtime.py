"""Process-wide resources, and the lifecycle phase 2 deliberately left out.

One engine, one HTTP client and one fetcher for the whole process, built when the bot
starts and closed when it stops.

**Why this is not built in `run()`.** An async engine and an `httpx.AsyncClient` bind
themselves to the event loop that is running when they are created. `run_polling` owns
the loop and starts it itself, so anything constructed before that call belongs to a
different loop — or to no loop at all — and the symptom arrives much later as a hang or
a `got Future attached to a different loop` from inside a handler. PTB's `post_init`
runs after the loop exists and before the first update is pulled, which is the only
window where this is correct.

**Why one fetcher and not one per command.** `HttpFetcher` holds the `robots.txt` cache
and the time of the last request per host. A fetcher built per `/add` remembers neither,
so the robots file is re-fetched every time and the spacing between requests silently
becomes zero — the conduct rules in the README would still be written down and would
stop being true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.error import TelegramError
from telegram.ext import Application

from price_tracker.bot import copy
from price_tracker.config import Settings, get_settings
from price_tracker.db import create_engine, create_session_factory
from price_tracker.scrapers import Fetcher, HttpFetcher

logger = logging.getLogger(__name__)

#: Where the resources live in `Application.bot_data`, which is the dict PTB hands to
#: every handler through `context`. A module-level global would work until the first
#: test wanted two bots, or one wanted none.
RESOURCES_KEY = "price_tracker_resources"


@dataclass(slots=True)
class Resources:
    """What a command needs: somewhere to read pages, somewhere to keep them.

    `http_client` is held only so it can be closed. The fetcher is the thing commands
    use, and it is typed as the `Fetcher` ABC rather than as `HttpFetcher` so a test can
    put a saved page behind it without a network — which is how every test in this repo
    exercises `/add`.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    fetcher: Fetcher
    http_client: httpx.AsyncClient | None = None

    @classmethod
    def build(cls, settings: Settings) -> Resources:
        """Open everything. Must be called with the event loop already running."""
        engine = create_engine(settings.database_url)
        client, fetcher = HttpFetcher.build()
        return cls(
            engine=engine,
            sessions=create_session_factory(engine),
            fetcher=fetcher,
            http_client=client,
        )

    async def aclose(self) -> None:
        """Close everything, and do not let the first failure strand the rest.

        Shutdown runs when something has usually already gone wrong, so each half is
        closed independently: an engine left undisposed holds its connections open, and
        on Postgres that is a connection leak that outlives the process it came from.
        """
        if self.http_client is not None:
            try:
                await self.http_client.aclose()
            except Exception:
                logger.exception("failed to close the HTTP client")
        await self.engine.dispose()


def resources_of(bot_data: dict[Any, Any]) -> Resources:
    """Pull the resources out of `context.bot_data`, or say why they are not there."""
    resources = bot_data.get(RESOURCES_KEY)
    if not isinstance(resources, Resources):
        raise RuntimeError(
            "bot resources are missing: the Application was built without post_init, "
            "so nothing opened the database. See price_tracker.bot.runtime."
        )
    return resources


async def post_init(application: Application) -> None:
    """Open the database and the HTTP client, then publish the command menu."""
    application.bot_data[RESOURCES_KEY] = Resources.build(get_settings())
    logger.info("resources ready")
    await _publish_command_menu(application)


async def post_shutdown(application: Application) -> None:
    """Close what `post_init` opened. Safe to run when `post_init` never did."""
    resources = application.bot_data.pop(RESOURCES_KEY, None)
    if isinstance(resources, Resources):
        await resources.aclose()
        logger.info("resources closed")


async def _publish_command_menu(application: Application) -> None:
    """Tell Telegram which commands to offer in the client's menu.

    Cosmetic, and therefore not allowed to stop the bot: this is one HTTP call to an
    API that is occasionally slow, and a bot that refuses to start because a menu did
    not update would be trading a working bot for a tidy one.
    """
    try:
        await application.bot.set_my_commands(copy.COMMAND_MENU)
    except TelegramError:
        logger.warning("could not publish the command menu", exc_info=True)
