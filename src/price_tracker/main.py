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
    "Todavía estoy en construcción: por ahora solo sé saludar.\n"
    "Pronto vas a poder mandarme el link de un producto y te aviso cuando baje."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    # Logged so that "does /start actually reply?" can be answered from the log rather
    # than by a person watching a phone. It went unverified across three sessions for
    # exactly the want of this line.
    logger.info("/start from chat %s", update.effective_chat.id if update.effective_chat else "?")
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
