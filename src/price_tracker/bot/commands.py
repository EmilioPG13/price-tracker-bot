"""The three commands, as functions that return the reply text.

Nothing in this module imports `telegram`. A command is a unit of work against the
scraper and the database, and everything that goes wrong inside one — a price that will
not parse, a store that refuses, an index pointing at nothing — has no relationship to
Telegram. The adapters in `handlers.py` are the half that knows about updates, chats and
the 4096-character message limit, and keeping that half thin is what lets these run in a
test without a single `telegram.Update` being built.

**The fetch happens outside the session.** Reading a store page takes seconds — a
robots.txt lookup, then a deliberate pause — and holding a transaction open across it
would mean the slowest thing the bot does is also the thing holding locks. So a command
fetches first and then opens exactly one `session_scope`, which is the unit of work: it
commits at the end, or rolls back if anything raised on the way.

**Every command is first contact.** `get_or_create_user` is called by `/list` and
`/remove` too, not only by `/add`. There is no sign-up step in this bot, and a `/list`
from someone unknown is a perfectly good introduction that happens to have nothing to
show yet.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from price_tracker.bot import charts, copy
from price_tracker.bot.runtime import Resources
from price_tracker.db import Repository, session_scope, utc_now
from price_tracker.money import to_cents
from price_tracker.scrapers import LayoutChangedError, ScraperError, fetch_product

logger = logging.getLogger(__name__)

#: How far back `/chart` looks. Long enough to show a seasonal sale, short enough that
#: a product tracked for a year does not render as a smear.
CHART_WINDOW = timedelta(days=90)

#: Below this a chart is worse than no chart: one reading is a dot in an empty box, and
#: a user who just ran `/add` would get exactly that.
MIN_CHART_POINTS = 2


@dataclass(frozen=True, slots=True)
class Chart:
    """A rendered chart and the text that goes under it.

    A command returns this instead of a `str` when it has a picture to send. That is the
    only reason `handlers.py` ever branches on a return value, and it is worth the
    branch: the alternative is `commands.py` learning how to send a photo, which is the
    one thing this module is built not to know.
    """

    png: bytes
    caption: str


async def add_tracking(resources: Resources, telegram_id: int, args: Sequence[str]) -> str:
    """`/add <link> <precio>` — read the page, then remember the product and the target.

    **The fetch cannot be skipped.** A product is `(store, external_id)`, and Cyberpuerta
    does not put its `external_id` anywhere in a URL — only in the page. So there is no
    way to notice "we already track this" before the page has been read. Deduping on the
    URL first is the natural optimisation and it would key the table on the one field
    the store demonstrably rewrites.

    Exactly two arguments, and extra ones are a usage error rather than something to
    guess at. A pasted price is one token (`1,899.00`); three tokens means something
    unintended happened, and quietly using the first two would act on a command the user
    did not give.
    """
    if len(args) != 2:
        return copy.ADD_USAGE

    raw_url, raw_target = args
    try:
        target_cents = to_cents(raw_target)
    except ValueError:
        return copy.bad_price(raw_target)
    if target_cents == 0:
        return copy.ZERO_TARGET

    try:
        data = await fetch_product(raw_url, resources.fetcher)
    except LayoutChangedError as error:
        # The only error in the hierarchy that means *this repo* is out of date, so it
        # is the only one worth a stack trace. The others are the store's weather.
        logger.exception("parser failed on %s", raw_url)
        return copy.reply_for_error(error)
    except ScraperError as error:
        logger.info("could not read %s: %s", raw_url, type(error).__name__)
        return copy.reply_for_error(error)

    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        user = await repo.get_or_create_user(telegram_id)
        product = await repo.upsert_product(data)
        tracking = await repo.set_tracking(user, product, target_cents)
        # The page was just read, so this is a genuine observation and belongs in the
        # history — including when the product was already known. Re-adding something
        # is not a reason to throw away a reading that cost a request.
        await repo.record_success(product, data)
        # `/add` is a price reading like any other, so it goes through the same door the
        # phase 4 checker will use. The point is the alert bookkeeping: if this price is
        # already at or under the target, the reply below *is* the alert, and consuming
        # it here is what stops the checker announcing the same thing six hours later.
        alerting = tracking.register_price(data.price_cents)

    logger.info(
        "tracking %s/%s for telegram_id=%s at %s cents",
        data.store,
        data.external_id,
        telegram_id,
        target_cents,
    )
    return copy.added(data, target_cents, alerting=alerting)


async def list_trackings(resources: Resources, telegram_id: int) -> str:
    """`/list` — everything this user watches, numbered for `/remove`.

    Formatting happens inside the session. Sessions are built with
    `expire_on_commit=False` so the rows would survive the exit, but reading them in
    here keeps that a convenience rather than something the display code depends on.
    """
    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        user = await repo.get_or_create_user(telegram_id)
        trackings = await repo.list_trackings(user)
        if not trackings:
            return copy.LIST_EMPTY
        return copy.tracking_list(trackings)


async def price_chart(resources: Resources, telegram_id: int, args: Sequence[str]) -> str | Chart:
    """`/chart <número>` — the price history of the product at that position in `/list`.

    Takes a position for the same reason `/remove` does, and resolves it the same way:
    against a list re-read here, not against whatever the user last saw on screen.

    **The drawing happens after the session closes.** Rendering a PNG is real CPU work
    handed to a thread, and there is no reason for a transaction to be open while it
    runs — the same instinct that keeps the store fetch out of `/add`'s session. What
    crosses the boundary is a list of plain values, not rows.
    """
    if len(args) != 1:
        return copy.CHART_USAGE
    try:
        position = int(args[0])
    except ValueError:
        return copy.CHART_USAGE

    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        user = await repo.get_or_create_user(telegram_id)
        trackings = await repo.list_trackings(user)
        if not 1 <= position <= len(trackings):
            return copy.CHART_NOT_FOUND

        tracking = trackings[position - 1]
        product = tracking.product
        history = await repo.price_history(product.id, since=utc_now() - CHART_WINDOW)

        points = [
            charts.PricePoint(
                checked_at=entry.checked_at,
                price_cents=entry.price_cents,
                in_stock=entry.in_stock,
            )
            for entry in history
        ]
        name = product.name
        currency = product.currency
        target_cents = tracking.target_price_cents

    if len(points) < MIN_CHART_POINTS:
        return copy.CHART_NOT_ENOUGH

    png = await charts.render_price_chart(
        points, name=name, currency=currency, target_cents=target_cents
    )
    prices = [point.price_cents for point in points]
    logger.info("charted product_id=%s for telegram_id=%s", product.id, telegram_id)
    return Chart(
        png=png,
        caption=copy.chart_caption(
            name=name,
            current_cents=prices[-1],
            low_cents=min(prices),
            high_cents=max(prices),
            target_cents=target_cents,
            currency=currency,
            observations=len(points),
        ),
    )


async def remove_tracking(resources: Resources, telegram_id: int, args: Sequence[str]) -> str:
    """`/remove <número>` — stop watching the product at that position in `/list`.

    A position, not an id. Showing database ids to a user would make the command exact
    and the list unreadable, and this is a bot someone types into on a phone.

    The cost is that a position means whatever the last `/list` said, and the list can
    move under it — the user adds something, then removes "number 2" from a listing that
    no longer exists. Two things keep that honest: the list is re-read here, inside the
    same transaction that does the delete, so the position is resolved against the
    current state rather than against a remembered one; and the confirmation names the
    product that went, so a mistake is visible immediately instead of next week.
    """
    if len(args) != 1:
        return copy.REMOVE_USAGE
    try:
        position = int(args[0])
    except ValueError:
        return copy.REMOVE_USAGE

    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        user = await repo.get_or_create_user(telegram_id)
        trackings = await repo.list_trackings(user)
        if not 1 <= position <= len(trackings):
            return copy.REMOVE_NOT_FOUND

        tracking = trackings[position - 1]
        # Read before the delete: the instance is detached from its row afterwards.
        name = tracking.product.name
        await repo.remove_tracking(user, tracking.product_id)

    logger.info("telegram_id=%s stopped tracking product_id=%s", telegram_id, tracking.product_id)
    return copy.removed(name)
