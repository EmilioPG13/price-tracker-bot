"""The scheduled half: read every product that is due, and tell people what changed.

This is the second caller of the service layer, and the reason that layer exists. It
goes through the same `scrapers.fetch_product()` and the same `Repository` as the `/add`
command, so the two runtimes cannot drift into two different ideas of what recording a
price means. Like `commands.py`, nothing here imports `telegram`: sending is a callable
the caller supplies, and `jobs.py` is the four-line adapter that fills it in with
`bot.send_message`. That keeps this whole module testable with a list as the outbox.

**Three transactions per product, not one.** The due list is read and its session
closed; the page is fetched with nothing held open; the result is written in a second
session. Holding a transaction across a store request would mean the slowest thing the
bot does is also the thing holding locks — the same reason `/add` fetches before it
opens a session — and it is worse here, because a run walks many products in a row. The
cost is that the `Product` row cannot be carried across the gap: it is re-loaded by id
through `Repository.get_product`, and may legitimately be gone by then.

**Retry lives here, and only for a 5xx.** `HttpFetcher` deliberately retries nothing,
because it has no idea how long the caller may wait; this layer is scheduled every few
hours and can afford thirty seconds. The narrowness is the conduct rule from the README,
not caution: a 5xx is the store telling us it is broken, and waiting is the cooperative
response. A 403 is the store telling us to go away, and asking again is just hammering —
per `docs/store-viability.md` a refusal is a finding to record, not an obstacle to work
around. A timeout is not retried either, because it is the one failure that might mean
we are the problem, and the next run in six hours is already the retry.

**Alerts are committed before they are sent, and that is a real trade.** The alternative
is to send inside the transaction, which holds it open across a call to Telegram and
risks the opposite failure: a message delivered and then rolled back, so the user hears
about the same drop again on the next run. Committing first inverts the risk — if the
send fails, the bookkeeping already says the user was told, and they will not hear about
this price again unless it falls further. That is the better half of a bad pair: a
missed alert is one silent message, a rollback storm is a bot that cries wolf, and the
alert rule's whole design is about not being noisy. The failure is logged loudly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from price_tracker.bot import copy
from price_tracker.bot.runtime import Resources
from price_tracker.db import Repository, session_scope
from price_tracker.scrapers import (
    Fetcher,
    LayoutChangedError,
    PageGoneError,
    ProductData,
    ScraperError,
    StoreUnavailableError,
    fetch_product,
)

logger = logging.getLogger(__name__)

#: How long to wait before each retry of a 5xx, in seconds. Two retries, spaced widely
#: enough to outlast a deploy and not so widely that a run of failing products turns one
#: pass into an hour. An empty tuple means a single attempt, which is what most tests
#: want and what makes the retry itself easy to assert on.
RETRY_BACKOFF: tuple[float, ...] = (5.0, 30.0)

#: Called with a Telegram id and the text to send it. The seam that keeps this module
#: free of `telegram`: a test passes a list's `append`, `jobs.py` passes the real bot.
Notifier = Callable[[int, str], Awaitable[None]]


@dataclass(slots=True)
class CheckRun:
    """What one pass did.

    Returned rather than only logged, because a scheduled job that reports nothing is a
    job nobody notices has stopped working. `crashed` counts products lost to an
    unexpected exception; a test asserting it is zero is how an accidental bug in the
    loop stops being invisible.
    """

    checked: int = 0
    succeeded: int = 0
    failed: int = 0
    retired: int = 0
    alerted: int = 0
    crashed: int = 0

    def summary(self) -> str:
        return (
            f"checked={self.checked} ok={self.succeeded} failed={self.failed} "
            f"retired={self.retired} alerted={self.alerted} crashed={self.crashed}"
        )


async def check_all_prices(
    resources: Resources,
    notify: Notifier,
    *,
    interval: timedelta,
    limit: int | None = None,
    backoff: Sequence[float] = RETRY_BACKOFF,
) -> CheckRun:
    """Check every product due for a reading, and alert whoever asked to be told.

    `interval` is how stale a product has to be before it is worth a request, and it is
    the real rate limiter — not the schedule. A bot that restarts ten times an hour runs
    this ten times and fetches nothing, because nothing has aged past the interval.

    `limit` caps one pass. Requests to a store are spaced by `HttpFetcher`, so a hundred
    due products is several minutes of deliberate waiting; stopping early leaves the
    oldest handled and the rest first in line next time, which is exactly the order
    `products_due_for_check` already returns.
    """
    async with session_scope(resources.sessions) as session:
        due = await Repository(session).products_due_for_check(interval=interval, limit=limit)
        # Taken out as plain values, on purpose. The session closes here and these rows
        # would be detached for the rest of the run; carrying ORM instances across a
        # network call and into another session is the kind of thing that works until
        # somebody adds a relationship to the loop.
        targets = [(product.id, product.canonical_url) for product in due]

    run = CheckRun()
    if not targets:
        logger.info("price check: nothing due")
        return run

    logger.info("price check: %s product(s) due", len(targets))
    for product_id, url in targets:
        run.checked += 1
        try:
            await _check_one(resources, notify, product_id, url, backoff=backoff, run=run)
        except Exception:
            # One product must not take the run down with it. Nobody is watching a
            # scheduled job, so a crash halfway through would silently leave the rest
            # unchecked until the next pass — and being the last product in the list is
            # not a reason to be skipped. `handlers.on_error` does the same for commands.
            run.crashed += 1
            logger.exception("price check crashed on product_id=%s", product_id)

    logger.info("price check done: %s", run.summary())
    return run


async def _check_one(
    resources: Resources,
    notify: Notifier,
    product_id: int,
    url: str,
    *,
    backoff: Sequence[float],
    run: CheckRun,
) -> None:
    """One product, start to finish: fetch, record, then send whatever it earned."""
    try:
        data = await _fetch_with_retry(url, resources.fetcher, backoff=backoff)
    except ScraperError as error:
        await _record_failure(resources, product_id, error, run=run)
        return

    alerts = await _record_success(resources, product_id, data, run=run)
    for telegram_id, text in alerts:
        await _send(notify, telegram_id, text, run=run)


async def _fetch_with_retry(url: str, fetcher: Fetcher, *, backoff: Sequence[float]) -> ProductData:
    """Read one page, retrying only while the store answers 5xx.

    Every other `ScraperError` propagates on the first attempt. The loop runs one more
    time than there are delays — three attempts for two delays — so the last wait is
    followed by a real try rather than by giving up on the one the wait was for.
    """
    for delay in backoff:
        try:
            return await fetch_product(url, fetcher)
        except StoreUnavailableError as error:
            logger.info("%s — retrying in %ss", error, delay)
            await asyncio.sleep(delay)

    return await fetch_product(url, fetcher)


async def _record_success(
    resources: Resources, product_id: int, data: ProductData, *, run: CheckRun
) -> list[tuple[int, str]]:
    """Write one good reading, and work out who should hear about it.

    Returns the messages to send rather than sending them, so the transaction closes
    before anything touches Telegram. See the module docstring for why that order.
    """
    alerts: list[tuple[int, str]] = []

    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        product = await repo.get_product(product_id)
        if product is None:
            logger.info("product_id=%s vanished mid-run; dropping the reading", product_id)
            return []

        # Read before `record_success` overwrites it. This is the number the alert shows
        # as "antes", and it is the only moment it still exists.
        previous_cents = product.last_price_cents
        await repo.record_success(product, data)

        for tracking in await repo.trackings_for_product(product_id):
            # The whole rule, including the re-arm on the way up, lives in here. The
            # checker never touches `last_alerted_*` itself — see `db/models.py`.
            if tracking.register_price(data.price_cents):
                alerts.append(
                    (
                        tracking.user.telegram_id,
                        copy.price_alert(data, tracking.target_price_cents, previous_cents),
                    )
                )

    # Counted here rather than inside the block: past the `async with` is the only point
    # at which the commit has actually happened, and a reading that was rolled back is
    # not a reading. The same run would otherwise report it as both a success and a crash.
    run.succeeded += 1
    if alerts:
        logger.info("product_id=%s triggered %s alert(s)", product_id, len(alerts))
    return alerts


async def _record_failure(
    resources: Resources, product_id: int, error: ScraperError, *, run: CheckRun
) -> None:
    """Write one failed check, and retire the product if that was the last straw."""
    run.failed += 1

    if isinstance(error, LayoutChangedError):
        # The only error in the hierarchy that means *this repo* is out of date, so the
        # only one worth a stack trace. `exc_info=error` rather than `logger.exception`:
        # this is outside the except block, so there is no current exception to pick up.
        logger.error("parser failed on product_id=%s", product_id, exc_info=error)
    else:
        logger.info("product_id=%s failed: %s", product_id, type(error).__name__)

    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        product = await repo.get_product(product_id)
        if product is None:
            return

        # A 404 is the one failure that does not get better by waiting, so it retires the
        # product immediately instead of spending five more requests proving the point.
        retired = await repo.record_failure(product, permanent=isinstance(error, PageGoneError))
        if retired:
            run.retired += 1
            logger.warning(
                "product_id=%s retired after %s consecutive failures (%s)",
                product_id,
                product.consecutive_failures,
                type(error).__name__,
            )


async def _send(notify: Notifier, telegram_id: int, text: str, *, run: CheckRun) -> None:
    """Deliver one alert. A failed delivery is logged and does not stop the run.

    Catches `Exception` rather than `TelegramError` deliberately: the notifier belongs
    to the caller, and naming its exceptions here would drag `telegram` back into the
    module that was built not to import it.
    """
    try:
        await notify(telegram_id, text)
    except Exception:
        logger.exception("could not deliver an alert to telegram_id=%s", telegram_id)
        return
    run.alerted += 1
