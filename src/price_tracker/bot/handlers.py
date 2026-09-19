"""The Telegram side: pull the arguments out of an update, send the answer back.

Every handler here is the same four steps — find the user, call a function in
`commands.py`, send what it returned — and that is the point. Anything longer than that
is logic that belongs on the other side of the seam, where it can be tested without
constructing a `telegram.Update`.
"""

from __future__ import annotations

import logging

from telegram import LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from price_tracker.bot import commands, copy, runtime

logger = logging.getLogger(__name__)

#: Telegram rejects a longer message outright, with a 400.
MESSAGE_LIMIT = 4096

#: A `/list` of five products would otherwise render five link cards, and a single
#: message gets a preview of whichever link Telegram picked. Neither is wanted.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record every update before any handler filters it.

    Registered in group -1, so it runs ahead of the real handlers and does not block
    them. It exists because "did anything reach this bot?" and "did the handler run?"
    are different questions, and only the first one tells you whether you are typing
    into the right chat — which is the thing that actually went wrong the first three
    times someone tried to verify `/start`.

    Phase 2's note suggested demoting this to DEBUG once real commands produced log
    lines of their own. It stays at INFO, because the case it is for is precisely the
    one where no command runs: a message matching no handler leaves no other trace.
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
    if update.effective_message is None:
        return
    await _reply(update.effective_message, copy.WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await _reply(update.effective_message, copy.HELP)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, telegram_id = _sender(update)
    if message is None or telegram_id is None:
        return

    # This one goes to the network: a robots.txt lookup, a polite pause, then the page.
    # Several seconds of silence in a chat reads as a broken bot.
    await _show_typing(message)
    text = await commands.add_tracking(
        runtime.resources_of(context.bot_data), telegram_id, context.args or []
    )
    await _reply(message, text)


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, telegram_id = _sender(update)
    if message is None or telegram_id is None:
        return

    text = await commands.list_trackings(runtime.resources_of(context.bot_data), telegram_id)
    await _reply(message, text)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, telegram_id = _sender(update)
    if message is None or telegram_id is None:
        return

    text = await commands.remove_tracking(
        runtime.resources_of(context.bot_data), telegram_id, context.args or []
    )
    await _reply(message, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last resort: log the traceback and tell the user something broke.

    Without this, PTB logs the exception and the chat stays silent, which is the worst
    of both — the user retries a command that will fail again, and nobody watching the
    chat knows anything happened.
    """
    logger.error("unhandled error while processing an update", exc_info=context.error)

    message = update.effective_message if isinstance(update, Update) else None
    if message is None:
        return
    try:
        await message.reply_text(copy.UNEXPECTED, link_preview_options=NO_PREVIEW)
    except TelegramError:
        # Apologising failed too. Nothing left to try, and raising from inside the error
        # handler would be a loop.
        logger.warning("could not deliver the error message", exc_info=True)


def _sender(update: Update) -> tuple[Message | None, int | None]:
    """The message and the Telegram user behind an update, if it has both.

    A command can arrive on an update with no message (an edited post, a channel) or
    with no user. Neither is something to answer, and neither is an error.
    """
    user = update.effective_user
    return update.effective_message, user.id if user else None


async def _show_typing(message: Message) -> None:
    """Best effort. A failed typing indicator must not take the command down with it."""
    try:
        await message.chat.send_action(ChatAction.TYPING)
    except TelegramError:
        logger.debug("could not send the typing action", exc_info=True)


async def _reply(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply_text(chunk, link_preview_options=NO_PREVIEW)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut `text` into pieces Telegram will accept, at the best boundary available.

    A long `/list` is the realistic way to reach 4096 characters, and the way it fails
    matters: Telegram rejects the whole message with a 400 rather than truncating it, so
    the user gets nothing at all rather than most of it.

    Three boundaries, in order of preference. **Blank lines first**, because that is
    what separates one product from the next — a chunk starting at "Ahora: $889.00" with
    no product name above it is a worse message than two tidy ones. Then **line
    breaks**, for a single entry too big to fit. Then a **hard cut**, for a line longer
    than the whole limit, where there is no boundary left to respect.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue

        pieces = _split_block(block, limit)
        chunks.extend(pieces[:-1])
        current = pieces[-1]

    if current:
        chunks.append(current)
    return chunks


def _split_block(block: str, limit: int) -> list[str]:
    """One entry that does not fit in a message, cut on lines and then on nothing."""
    pieces: list[str] = []
    current = ""
    for raw_line in block.split("\n"):
        line = raw_line
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate

    if current:
        pieces.append(current)
    return pieces or [""]
