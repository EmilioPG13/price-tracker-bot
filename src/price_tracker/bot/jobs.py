"""The Telegram side of the scheduled checker.

`handlers.py` is to `commands.py` what this is to `checker.py`: the thin adapter that
knows about PTB, so the module doing the work does not have to. The symmetry is the
point — there are two runtimes and one service layer, and both adapters are short enough
to read in one go precisely because neither contains any logic.

**This is the first time the bot talks *to* Telegram rather than being talked to.** A
command has a message to reply to; an alert has only a user id, which is also the chat
id for a private chat. That is the whole difference, and it is why `checker.py` takes a
notifier rather than returning strings the way `commands.py` does — there is nobody
waiting on the other end of a return value.

**The job queue is not optional.** `JobQueue` lives behind the `[job-queue]` extra, and
without it `application.job_queue` is `None`. Skipping the schedule with a warning would
produce a bot that answers every command perfectly and never alerts anyone — the failure
would surface as silence, hours later, which is the worst way for a missing dependency
to announce itself. So it raises at startup instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from telegram.ext import Application, ContextTypes

from price_tracker.bot import checker, handlers, runtime

logger = logging.getLogger(__name__)

#: How long after startup the first check runs. Not zero: a bot that restarts should
#: come up and answer commands before it starts spending seconds on store requests, and
#: a crash-loop should not turn into a request loop. It is only a courtesy either way —
#: `products_due_for_check` is the real rate limiter, and nothing is due until it is.
FIRST_RUN_DELAY = timedelta(seconds=60)

JOB_NAME = "check-prices"


def schedule(application: Application, *, interval: timedelta, limit: int | None) -> None:
    """Register the recurring price check on the application's job queue.

    The interval is passed through `job.data` rather than read from settings inside the
    callback. It decides which products are due *and* how often the job runs, and those
    two have to be the same number — reading it in two places is how they stop being.
    """
    if application.job_queue is None:
        raise RuntimeError(
            "no job queue: python-telegram-bot is installed without the [job-queue] "
            "extra, so the price checker would never run. See pyproject.toml."
        )

    application.job_queue.run_repeating(
        check_prices,
        interval=interval,
        first=FIRST_RUN_DELAY,
        name=JOB_NAME,
        data=(interval, limit),
    )
    logger.info("price check scheduled every %s (limit=%s)", interval, limit)


async def check_prices(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The scheduled job. Unwraps the context and hands over to `checker`."""
    job = context.job
    interval, limit = job.data if job is not None and job.data is not None else (None, None)
    if interval is None:
        raise RuntimeError("the price check job was scheduled without an interval")

    await checker.check_all_prices(
        runtime.resources_of(context.bot_data),
        _notifier(context),
        interval=interval,
        limit=limit,
    )


def _notifier(context: ContextTypes.DEFAULT_TYPE) -> checker.Notifier:
    """Build the send-one-alert callable the checker calls.

    Reuses `handlers.split_message` and `handlers.NO_PREVIEW` rather than restating
    them. An alert is shorter than a `/list` and will not hit the 4096-character limit
    today, but "today" is exactly the qualifier that makes a second, divergent way of
    sending a message worth avoiding.

    **Nothing is caught here.** A failed send has to reach the checker, because the
    checker is what counts alerts as delivered — swallowing a `Forbidden` from a user
    who blocked the bot would report an alert nobody received. So this stays four lines
    and the one `except` in the system stays in `checker._send`.
    """

    async def send(telegram_id: int, text: str) -> None:
        for chunk in handlers.split_message(text):
            await context.bot.send_message(
                chat_id=telegram_id,
                text=chunk,
                link_preview_options=handlers.NO_PREVIEW,
            )

    return send
