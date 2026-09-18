"""The four tables, and the rules that are properties of a row rather than of a query.

Three of the choices here are load-bearing and none of them are obvious from the shape
of the classes, so they are worth stating before the code:

1. **Money is `Integer` cents.** Never `Numeric`. On SQLite, SQLAlchemy's `Numeric`
   round-trips through `float` and loses precision without raising, which is the exact
   failure `price_tracker.money` exists to prevent — storing it back through a lossy
   column would undo that work one layer down. See `docs/scraper-design.md`.

2. **Timestamps go through `UtcDateTime`.** SQLite has no timezone type at all, so a
   naive datetime written on one backend and an aware one read on another is a
   `TypeError` waiting for the first comparison. See the class below.

3. **A product is `(store, external_id)`, not a URL.** Cyberpuerta serves one SSD on
   three different URLs and disagrees with two of them; `docs/scraper-design.md` has
   the capture. A URL-keyed table would have held three rows for one product.

The alert rule lives here too, on `Tracking`, rather than in the phase 4 checker. It is
a function of a row's own state, and putting it on the row means the caller cannot
forget the half everybody forgets — see `Tracking.register_price`.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# A second alert for the same product within this window is suppressed, even if the
# price keeps falling. Twelve hours is a judgement call, not a measurement: it is short
# enough that a one-day sale is not missed and long enough that a store flapping between
# two prices cannot turn into a notification every few minutes.
ALERT_COOLDOWN = timedelta(hours=12)


def utc_now() -> datetime:
    """The project's only clock. Timezone-aware, always UTC."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp column that is timezone-aware UTC on every backend.

    SQLite has no timezone type: `DateTime(timezone=True)` is accepted and then quietly
    ignored, so a datetime written as aware comes back naive. Postgres returns it aware.
    Code that compares `row.last_alerted_at` against `datetime.now(UTC)` therefore works
    in production and raises `TypeError: can't compare offset-naive and offset-aware
    datetimes` in the tests, or the reverse — which is the worst possible split.

    This is the same discipline as `money.to_cents()`: one door in, and it refuses what
    it cannot convert exactly rather than guessing. A naive datetime is rejected on the
    way in, because guessing its zone is how a 12-hour cooldown becomes a 6-hour one.
    Re-attaching UTC on the way out is not a guess: nothing naive ever got stored.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime rejected: {value!r} — use price_tracker.db.utc_now()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ProductStatus(enum.StrEnum):
    """Whether the checker should still be spending a request on this product."""

    ACTIVE = "active"
    #: Retired by the checker: a 404/410, or too many consecutive failures. Kept rather
    #: than deleted so the price history and the user's tracking survive, and so the
    #: same URL pasted again does not silently start a fresh failure count.
    RETIRED = "retired"


# Every constraint gets a deterministic name. Without this, SQLite invents anonymous
# names for CHECK and UNIQUE constraints, and an Alembic downgrade that needs to DROP
# one has nothing to refer to — a problem that only appears the first time a migration
# has to be reversed, which is the worst time to discover it.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base. Alembic reads `Base.metadata` to autogenerate migrations."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    """One Telegram account. Created on first contact, never by an explicit sign-up."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # BigInteger, not Integer: Telegram ids already exceed 2^31 and the API documents
    # them as up to 52 bits. On SQLite every integer is 64-bit so the bug would only
    # appear in Postgres, i.e. only in production.
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    trackings: Mapped[list[Tracking]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        # Let the database do the cascading. Without this the ORM loads every child row
        # to delete it individually, which `lazy="raise_on_sql"` forbids — and which
        # would also mean the ON DELETE CASCADE in the schema is never exercised, so
        # nothing would ever catch SQLite's foreign keys being off. See `engine.py`.
        passive_deletes=True,
        # Async sessions cannot lazy-load: an unloaded attribute raises MissingGreenlet,
        # which says nothing useful. This raises at the access instead, naming the
        # relationship and pointing at the missing selectinload.
        lazy="raise_on_sql",
    )


class Product(Base):
    """One listing at one store, shared by every user tracking it.

    Deliberately not per-user. Two people tracking the same SSD cost one request, not
    two — which matters because the conduct rules in `docs/store-viability.md` cap us at
    one request per page.
    """

    __tablename__ = "products"
    __table_args__ = (
        # The rule the whole identity design exists to enforce.
        UniqueConstraint("store", "external_id", name="uq_products_store_external_id"),
        # The checker's only query: active products, oldest check first.
        Index("ix_products_status_last_checked_at", "status", "last_checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[str] = mapped_column(String(32))
    #: The store's own id for the listing. For Cyberpuerta this is the OXID `anid`, not
    #: the JSON-LD `sku` — see `docs/scraper-design.md` for why that distinction matters.
    external_id: Mapped[str] = mapped_column(String(128))
    #: For showing the user a link, refreshed on every check. A store may rewrite a slug
    #: without the product changing, so this is display data and never identity.
    canonical_url: Mapped[str] = mapped_column(String(2048))
    name: Mapped[str] = mapped_column(String(512))
    currency: Mapped[str] = mapped_column(String(3))

    #: NULL until the first successful check. Distinguishes "never read" from "free".
    last_price_cents: Mapped[int | None] = mapped_column(default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    #: Reset to 0 by any success. Only a run of failures retires a product, so one bad
    #: afternoon at the store does not delete somebody's tracking.
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    status: Mapped[ProductStatus] = mapped_column(
        # native_enum=False stores a VARCHAR with a CHECK constraint instead of a
        # Postgres ENUM type. Adding a value to a native ENUM needs its own migration
        # and cannot run inside a transaction on older Postgres; a CHECK is portable and
        # behaves the same on SQLite, which is what the tests run on.
        #
        # `values_callable` is not cosmetic. By default SQLAlchemy persists a Python
        # enum by its *name*, so `ProductStatus.ACTIVE` is stored as `ACTIVE` while the
        # member's value is `active` — and a `server_default` written as the value then
        # violates the CHECK constraint generated from the names. The ORM never hits it,
        # because it always sends the status explicitly; a raw INSERT or a data
        # migration does. Storing the lowercase values keeps the column, the default and
        # the constraint saying the same thing.
        Enum(
            ProductStatus,
            native_enum=False,
            length=16,
            create_constraint=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=ProductStatus.ACTIVE,
        server_default=ProductStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    trackings: Mapped[list[Tracking]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise_on_sql",
    )
    history: Mapped[list[PriceHistory]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise_on_sql",
    )


class Tracking(Base):
    """One user watching one product at one target price.

    Unique on `(user_id, product_id)`: asking twice for the same product is a change of
    target, not a second row, and two rows would mean two alerts for one price drop.
    """

    __tablename__ = "trackings"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_trackings_user_id_product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    target_price_cents: Mapped[int]

    #: The price the user was last told about. NULL means "armed": the next price at or
    #: under target alerts. See `register_price` for why it is cleared on the way up.
    last_alerted_price_cents: Mapped[int | None] = mapped_column(default=None)
    last_alerted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    user: Mapped[User] = relationship(back_populates="trackings", lazy="raise_on_sql")
    product: Mapped[Product] = relationship(back_populates="trackings", lazy="raise_on_sql")

    def should_alert(self, price_cents: int, *, now: datetime | None = None) -> bool:
        """Whether this price reading is worth a message. Pure: changes nothing.

        Three conditions, and the second is the one that keeps a long sale from becoming
        a daily notification: after alerting at 889, only a price *below* 889 qualifies.
        """
        if price_cents > self.target_price_cents:
            return False
        if (
            self.last_alerted_price_cents is not None
            and price_cents >= self.last_alerted_price_cents
        ):
            return False
        if self.last_alerted_at is not None:
            moment = now if now is not None else utc_now()
            if moment - self.last_alerted_at < ALERT_COOLDOWN:
                return False
        return True

    def register_price(self, price_cents: int, *, now: datetime | None = None) -> bool:
        """Apply one price reading to this tracking. Returns whether to alert now.

        This exists as one call rather than a predicate the caller pairs with a setter,
        because the rule has a half that is easy to omit: **when the price goes back
        above target, `last_alerted_price_cents` is cleared.** Without that reset, a
        product that dropped to 889, recovered to 1200 and fell to 950 would stay silent
        forever, since 950 is not below the 889 we last announced. Re-arming on the way
        up is what makes the next drop a new event instead of a worse version of the old
        one.

        The caller is responsible for committing; this only mutates the instance.
        """
        if price_cents > self.target_price_cents:
            self.last_alerted_price_cents = None
            return False

        moment = now if now is not None else utc_now()
        if not self.should_alert(price_cents, now=moment):
            return False

        self.last_alerted_price_cents = price_cents
        self.last_alerted_at = moment
        return True


class PriceHistory(Base):
    """One observation. Append-only; nothing in the application updates a row here.

    `in_stock` is recorded alongside the price because phase 1 settled that an
    out-of-stock product is data rather than an error — it keeps its price and flips
    availability, and it may come back cheaper. A chart of price alone would draw a flat
    line through a period when the thing could not be bought at all.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        # The chart query in phase 4: one product, a window of time, in order.
        Index("ix_price_history_product_id_checked_at", "product_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    price_cents: Mapped[int]
    in_stock: Mapped[bool] = mapped_column(default=True)
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    product: Mapped[Product] = relationship(back_populates="history", lazy="raise_on_sql")
