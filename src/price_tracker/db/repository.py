"""The seam between the scraper and the database.

Everything the application does to persistent state goes through this class, for the
same reason `scrapers.fetch_product()` is the only way to read a page: there are two
runtimes — the Telegram command handler and the scheduled checker — and they must not
grow two different ideas of what "record a price" means. Phase 4's `check_all_prices()`
is a loop over `products_due_for_check`, `record_success` and `record_failure`, and it
should not contain a single `select()` of its own.

The repository takes a session rather than making one. Who owns the transaction is the
caller's business: the `/add` handler wraps one command, the checker wraps one product
at a time so a store failing halfway does not discard the prices already read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from price_tracker.db.models import (
    PriceHistory,
    Product,
    ProductStatus,
    Tracking,
    User,
    utc_now,
)
from price_tracker.scrapers.base import ProductData

# How many checks in a row must fail before a product stops being checked. At the
# default six-hour interval that is a day and a half of a store being unreachable,
# which is long enough to outlast an outage and short enough not to spend requests on a
# page that has genuinely gone.
MAX_CONSECUTIVE_FAILURES = 6


class Repository:
    """Persistent state, as the application sees it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- users -------------------------------------------------------------

    async def get_or_create_user(self, telegram_id: int) -> User:
        """Find the user behind a Telegram id, creating them on first contact.

        There is no sign-up step in this bot: the first `/add` is the registration.
        """
        user = await self.session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is not None:
            return user

        user = User(telegram_id=telegram_id)
        self.session.add(user)
        await self.session.flush()
        return user

    # ---- products ----------------------------------------------------------

    async def upsert_product(self, data: ProductData) -> Product:
        """Find or create the product `data` describes, and refresh its display fields.

        This is where `(store, external_id)` earns its place. The same SSD reaches us as
        a category URL, as the 301 target of that URL, and as the different canonical
        URL the store declares — three spellings, one row, because none of them is the
        key. `canonical_url` and `name` are overwritten on every sighting, since a store
        renaming a slug is not a new product.

        Note this does not record a price: a product being *known* and a product having
        been *checked* are different events, and `/add` does both in that order.
        """
        product = await self.session.scalar(
            select(Product).where(
                Product.store == data.store,
                Product.external_id == data.external_id,
            )
        )
        if product is None:
            product = Product(
                store=data.store,
                external_id=data.external_id,
                canonical_url=data.canonical_url,
                name=data.name,
                currency=data.currency,
            )
            self.session.add(product)
            await self.session.flush()
            return product

        product.canonical_url = data.canonical_url
        product.name = data.name
        product.currency = data.currency
        if product.status is ProductStatus.RETIRED:
            # Someone pasted it again and it parsed, so whatever retired it is over.
            product.status = ProductStatus.ACTIVE
            product.consecutive_failures = 0
        return product

    async def products_due_for_check(
        self, *, interval: timedelta, limit: int | None = None, now: datetime | None = None
    ) -> Sequence[Product]:
        """Active products not checked within `interval`, oldest first.

        Never-checked products come first: `nulls_first()` is explicit because the two
        backends disagree about where NULL sorts in an ascending order, and a checker
        that silently visits new products last is hard to notice.
        """
        moment = now if now is not None else utc_now()
        cutoff = moment - interval

        query = (
            select(Product)
            .where(
                Product.status == ProductStatus.ACTIVE,
                or_(Product.last_checked_at.is_(None), Product.last_checked_at <= cutoff),
            )
            .order_by(Product.last_checked_at.asc().nulls_first(), Product.id)
        )
        if limit is not None:
            query = query.limit(limit)

        return (await self.session.scalars(query)).all()

    async def record_success(
        self, product: Product, data: ProductData, *, now: datetime | None = None
    ) -> PriceHistory:
        """Record one successful reading: append to history, refresh the product.

        The failure counter is reset here rather than decremented. A product that has
        answered is healthy, regardless of how badly last week went.
        """
        moment = now if now is not None else utc_now()

        entry = PriceHistory(
            product_id=product.id,
            price_cents=data.price_cents,
            in_stock=data.in_stock,
            checked_at=moment,
        )
        self.session.add(entry)

        product.last_price_cents = data.price_cents
        product.last_checked_at = moment
        product.consecutive_failures = 0
        product.canonical_url = data.canonical_url
        product.name = data.name
        product.currency = data.currency

        await self.session.flush()
        return entry

    async def record_failure(
        self, product: Product, *, permanent: bool = False, now: datetime | None = None
    ) -> bool:
        """Record one failed check. Returns whether the product is now retired.

        `permanent=True` is for a `PageGoneError` — a 404 or 410 does not get better by
        waiting, so there is no reason to spend five more requests proving it. Everything
        else is transient and only retires the product once it has failed
        `MAX_CONSECUTIVE_FAILURES` times in a row.

        `last_checked_at` is updated on failure too. It is the checker's scheduling
        field, not a record of success; leaving it alone would make a failing product
        eligible again on every single run.
        """
        product.consecutive_failures += 1
        product.last_checked_at = now if now is not None else utc_now()

        if permanent or product.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            product.status = ProductStatus.RETIRED

        await self.session.flush()
        return product.status is ProductStatus.RETIRED

    async def price_history(
        self, product_id: int, *, since: datetime | None = None
    ) -> Sequence[PriceHistory]:
        """Observations for one product, oldest first. Phase 4 draws this."""
        query = select(PriceHistory).where(PriceHistory.product_id == product_id)
        if since is not None:
            query = query.where(PriceHistory.checked_at >= since)
        query = query.order_by(PriceHistory.checked_at, PriceHistory.id)

        return (await self.session.scalars(query)).all()

    # ---- trackings ---------------------------------------------------------

    async def set_tracking(self, user: User, product: Product, target_price_cents: int) -> Tracking:
        """Track a product for a user, or move the target if they already track it.

        Asking twice is a change of mind, not a second row — two rows would mean two
        messages for one price drop. Moving the target also re-arms the alert: a user
        who lowers their target is asking to be told again, and leaving
        `last_alerted_price_cents` set would silence exactly the drop they just asked for.
        """
        tracking = await self.session.scalar(
            select(Tracking).where(
                Tracking.user_id == user.id,
                Tracking.product_id == product.id,
            )
        )
        if tracking is None:
            tracking = Tracking(
                user_id=user.id,
                product_id=product.id,
                target_price_cents=target_price_cents,
            )
            self.session.add(tracking)
        elif tracking.target_price_cents != target_price_cents:
            tracking.target_price_cents = target_price_cents
            tracking.last_alerted_price_cents = None

        await self.session.flush()
        return tracking

    async def list_trackings(self, user: User) -> Sequence[Tracking]:
        """Everything a user watches, with the product loaded.

        `selectinload` is not an optimisation here. The relationships are declared
        `lazy="raise_on_sql"`, so reading `tracking.product.name` in a handler without
        this raises rather than quietly emitting a query the async session cannot run.
        """
        query = (
            select(Tracking)
            .where(Tracking.user_id == user.id)
            .options(selectinload(Tracking.product))
            .order_by(Tracking.created_at, Tracking.id)
        )
        return (await self.session.scalars(query)).all()

    async def trackings_for_product(self, product_id: int) -> Sequence[Tracking]:
        """Everyone watching one product, with their user loaded.

        The checker's other half: having read a price, this is who might hear about it.
        """
        query = (
            select(Tracking)
            .where(Tracking.product_id == product_id)
            .options(selectinload(Tracking.user))
            .order_by(Tracking.id)
        )
        return (await self.session.scalars(query)).all()

    async def remove_tracking(self, user: User, product_id: int) -> bool:
        """Stop watching. Returns whether there was anything to stop.

        The product row and its history stay: another user may be watching it, and even
        if nobody is, the history is cheap and re-adding the product later should not
        start from an empty chart.
        """
        result = await self.session.execute(
            delete(Tracking).where(
                Tracking.user_id == user.id,
                Tracking.product_id == product_id,
            )
        )
        return bool(result.rowcount)
