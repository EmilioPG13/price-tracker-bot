"""Bot entry point: settings in, a wired `Application` out.

Everything with behaviour lives in `price_tracker.bot`; this module is the wiring, and
it is meant to stay the shortest file in the package. `build_application` is separate
from `run` so the handler table can be asserted in a test without opening a socket.
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, TypeHandler

from price_tracker.bot import handlers, runtime
from price_tracker.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> Application:
    """Assemble the bot. Opens nothing — `post_init` does that once the loop is running.

    `post_init` and `post_shutdown` are where the engine and the HTTP client are opened
    and closed. They cannot be built here: both bind to the running event loop, and
    `run_polling` starts that loop after this function has already returned. See
    `bot.runtime` for what that failure looks like when it is got wrong.
    """
    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(runtime.post_init)
        .post_shutdown(runtime.post_shutdown)
        .build()
    )

    # Group -1 runs before group 0, so this sees every update — including the ones no
    # command below claims, which is the case it exists for.
    application.add_handler(TypeHandler(Update, handlers.log_update), group=-1)

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("add", handlers.add))
    application.add_handler(CommandHandler("list", handlers.show_list))
    application.add_handler(CommandHandler("remove", handlers.remove))

    application.add_error_handler(handlers.on_error)
    return application


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx logs every Telegram poll at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run() -> None:
    configure_logging()
    application = build_application(get_settings())

    logger.info("Bot starting (polling)")
    # run_polling owns the event loop; it is deliberately a sync call.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
