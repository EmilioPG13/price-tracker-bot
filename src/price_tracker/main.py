"""Bot entry point.

Phase 0 scope: prove the async stack answers on Telegram. Real commands land in phase 3.
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.config import get_settings

logger = logging.getLogger(__name__)

# User-facing copy is Spanish: the target stores and users are Mexican.
WELCOME = (
    "Hola. Soy un bot que vigila precios.\n\n"
    "Todavia estoy en construccion: por ahora solo se saludar.\n"
    "Pronto vas a poder mandarme el link de un producto y avisarte cuando baje."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(WELCOME)


def run() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx logs every Telegram poll at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    app = Application.builder().token(settings.bot_token).build()
    app.add_handler(CommandHandler("start", start))

    logger.info("Bot starting (polling)")
    # run_polling owns the event loop; it is deliberately a sync call.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
