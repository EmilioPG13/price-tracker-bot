"""Bot entry point.

Phase 0 scope: prove the async stack answers on Telegram. Real commands land in phase 3.
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler

from price_tracker.config import get_settings

logger = logging.getLogger(__name__)

# User-facing copy is Spanish: the target stores and users are Mexican.
WELCOME = (
    "Hola. Soy un bot que vigila precios.\n\n"
    "Todavía estoy en construcción: por ahora solo sé saludar.\n"
    "Pronto vas a poder mandarme el link de un producto y te aviso cuando baje."
)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record every update before any handler filters it.

    Registered in group -1, so it runs ahead of the real handlers and does not block
    them. It exists because "did anything reach this bot?" and "did the `/start` handler
    run?" are different questions, and only the first one tells you whether you are
    typing into the right chat — which is the thing that actually went wrong the first
    three times someone tried to verify this.

    Logging inside `start()` cannot answer it: a handler that returns early on an update
    shape it does not recognise leaves no trace at all.
    """
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    logger.info(
        "update %s | chat=%s user=@%s | %r",
        update.update_id,
        chat.id if chat else "?",
        user.username if user else "?",
        message.text if message else None,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(WELCOME)
    logger.info("replied to /start in chat %s", update.message.chat_id)


def run() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx logs every Telegram poll at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    app = Application.builder().token(settings.bot_token).build()
    # Group -1 runs before group 0, so this sees everything the handlers do — and
    # everything they do not. Phase 3 may want it at DEBUG once there are real commands
    # producing their own log lines; while `/start` is the only one, INFO is right.
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler("start", start))

    logger.info("Bot starting (polling)")
    # run_polling owns the event loop; it is deliberately a sync call.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
