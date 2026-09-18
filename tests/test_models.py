"""Tests for the schema itself, and for the rules that live on a row.

These defend the three things about this layer that fail silently: money stored through
a lossy column, a timestamp that loses its timezone, and a foreign key that is declared
and not enforced. None of the three raises on the way in, which is why each one gets a
test rather than a comment.
"""

import pytest
from sqlalchemy import Integer, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError, StatementError

from price_tracker.db import (
    ALERT_COOLDOWN,
    Base,
    PriceHistory,
    Product,
    ProductStatus,
    Tracking,
    User,
    utc_now,
)

MONEY_COLUMNS = [
    ("products", "last_price_cents"),
    ("trackings", "target_price_cents"),
    ("trackings", "last_alerted_price_cents"),
    ("price_history", "price_cents"),
]


@pytest.mark.parametrize(("table", "column"), MONEY_COLUMNS)
def test_money_is_stored_as_integer_cents(table, column):
    # Numeric is the trap: on SQLite it round-trips through float and loses precision
    # with nothing raised anywhere. `money.to_cents()` refuses a price it cannot read
    # exactly; a Numeric column would quietly undo that one layer down.
    kind = Base.metadata.tables[table].columns[column].type
    assert isinstance(kind, Integer), f"{table}.{column} is {kind!r}, not integer cents"


async def test_a_real_telegram_id_fits(session):
    # Telegram ids already exceed 2^31. On SQLite every integer is 64-bit, so an
    # Integer column would only overflow in Postgres — that is, only in production.
    user = User(telegram_id=7_284_119_365)
    session.add(user)
    await session.flush()

    assert await session.scalar(select(User.telegram_id)) == 7_284_119_365


async def test_a_naive_timestamp_is_refused(session):
    user = User(telegram_id=1, created_at=utc_now().replace(tzinfo=None))
    session.add(user)

    with pytest.raises(StatementError, match="naive datetime"):
        await session.flush()


async def test_a_timestamp_comes_back_aware_and_in_utc(session, engine):
    moment = utc_now()
    session.add(User(telegram_id=1, created_at=moment))
    await session.commit()

    # Expire everything so the value is genuinely read back from the database rather
    # than handed back out of the identity map, which would prove nothing.
    session.expunge_all()
    stored = await session.scalar(select(User.created_at))

    assert stored.tzinfo is not None
    assert stored == moment


async def test_one_product_cannot_be_stored_twice(session):
    for _ in range(2):
        session.add(
            Product(
                store="cyberpuerta",
                external_id="c156664ff9062de87fc3bf694dbb8eae",
                canonical_url="https://www.cyberpuerta.mx/a.html",
                name="SSD Kingston A400 240GB",
                currency="MXN",
            )
        )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_same_id_at_a_different_store_is_a_different_product(session):
    # `external_id` is only unique within a store. Two stores may well both use a plain
    # integer id, and nothing says they cannot collide.
    for store in ("cyberpuerta", "liverpool"):
        session.add(
            Product(
                store=store,
                external_id="1141535451",
                canonical_url=f"https://{store}.example/a",
                name="Same id, different shop",
                currency="MXN",
            )
        )
    await session.flush()

    assert len((await session.scalars(select(Product))).all()) == 2


async def test_a_user_cannot_track_one_product_twice(session):
    user = User(telegram_id=1)
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add_all([user, product])
    await session.flush()

    # Two rows would mean two messages for one price drop.
    session.add_all(
        [
            Tracking(user_id=user.id, product_id=product.id, target_price_cents=80000),
            Tracking(user_id=user.id, product_id=product.id, target_price_cents=70000),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_user_deletes_their_trackings(session):
    """Proves the SQLite foreign-key pragma in `engine.py` is actually on.

    Without `PRAGMA foreign_keys=ON`, this DELETE succeeds and leaves the tracking
    behind pointing at a user that no longer exists, with nothing raised. Postgres
    would have cascaded, so the bug would exist only where it is hardest to see.
    """
    user = User(telegram_id=1)
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add_all([user, product])
    await session.flush()
    session.add(Tracking(user_id=user.id, product_id=product.id, target_price_cents=80000))
    await session.commit()

    await session.delete(user)
    await session.commit()

    assert (await session.scalars(select(Tracking))).all() == []
    # The product survives: other people may be watching it.
    assert await session.scalar(select(Product.id)) == product.id


async def test_deleting_a_product_deletes_its_history(session):
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add(product)
    await session.flush()
    session.add(PriceHistory(product_id=product.id, price_cents=88900))
    await session.commit()

    await session.delete(product)
    await session.commit()

    assert (await session.scalars(select(PriceHistory))).all() == []


async def test_a_product_starts_active_and_unchecked(session):
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add(product)
    await session.flush()

    # NULL rather than 0: "never read" and "free" are different facts.
    assert product.last_price_cents is None
    assert product.last_checked_at is None
    assert product.consecutive_failures == 0
    assert product.status is ProductStatus.ACTIVE


async def test_an_unloaded_relationship_raises_instead_of_a_greenlet_error(session):
    user = User(telegram_id=1)
    session.add(user)
    await session.commit()

    session.expunge_all()
    reloaded = await session.scalar(select(User))

    # `lazy="raise_on_sql"` turns an inscrutable MissingGreenlet into a message naming
    # the relationship that needed a selectinload.
    with pytest.raises(InvalidRequestError, match="raise_on_sql"):
        _ = reloaded.trackings


async def test_the_status_default_satisfies_its_own_check_constraint(session):
    """A regression test for a bug the ORM path could not have found.

    SQLAlchemy persists a Python enum by its *name* unless told otherwise, so the CHECK
    constraint was generated over `ACTIVE`/`RETIRED` while the `server_default` was the
    member's value, `active`. Every ORM insert sends the status explicitly, so all of
    this layer's other tests passed; only a raw INSERT relying on the default — a data
    migration, or a fix-up in a shell — hit the constraint.
    """
    await session.execute(
        text(
            "INSERT INTO products "
            "(store, external_id, canonical_url, name, currency, consecutive_failures, "
            " created_at) "
            "VALUES ('cyberpuerta', 'abc', 'https://x', 'SSD', 'MXN', 0, '2026-01-01')"
        )
    )

    assert await session.scalar(text("SELECT status FROM products")) == "active"


async def test_the_status_column_stores_the_lowercase_value(session):
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add(product)
    await session.commit()

    # What is actually on disk, not what the ORM hands back.
    assert await session.scalar(text("SELECT status FROM products")) == "active"
    session.expunge_all()
    assert (await session.scalar(select(Product))).status is ProductStatus.ACTIVE


def test_the_status_column_is_a_check_not_a_native_enum():
    # A native Postgres ENUM needs its own migration to gain a value and, on older
    # servers, cannot be altered inside a transaction. A VARCHAR with a CHECK behaves
    # the same on SQLite and Postgres, which is what makes the test backend meaningful.
    kind = Base.metadata.tables["products"].columns["status"].type
    assert getattr(kind, "native_enum", True) is False


def test_the_checker_query_has_an_index_to_use():
    # `products_due_for_check` filters on status and orders by last_checked_at. This is
    # cheap to assert and the alternative is discovering it as a slow scan in phase 4.
    names = {index.name for index in Base.metadata.tables["products"].indexes}
    assert "ix_products_status_last_checked_at" in names


def test_every_table_the_design_calls_for_exists():
    assert set(Base.metadata.tables) == {"users", "products", "trackings", "price_history"}


# ---- the alert rule ------------------------------------------------------------


def make_tracking(**overrides) -> Tracking:
    """A detached tracking. The alert rule is pure, so it needs no database."""
    fields = {"user_id": 1, "product_id": 1, "target_price_cents": 90000}
    fields.update(overrides)
    return Tracking(**fields)


def test_a_price_at_the_target_alerts(now):
    assert make_tracking().register_price(90000, now=now) is True


def test_a_price_above_the_target_does_not_alert(now):
    assert make_tracking().register_price(90001, now=now) is False


def test_the_same_price_twice_alerts_once(now, hours):
    tracking = make_tracking()

    assert tracking.register_price(88900, now=now) is True
    # A day later, still on sale at the same price. Nothing new has happened.
    assert tracking.register_price(88900, now=now + hours(24)) is False


def test_a_lower_price_inside_the_cooldown_still_waits(now, hours):
    tracking = make_tracking()
    tracking.register_price(88900, now=now)

    # Genuinely cheaper, but a store flapping between two prices must not turn into a
    # notification every few minutes.
    assert tracking.register_price(80000, now=now + hours(1)) is False


def test_a_lower_price_after_the_cooldown_alerts(now, hours):
    tracking = make_tracking()
    tracking.register_price(88900, now=now)

    assert tracking.register_price(80000, now=now + hours(13)) is True
    assert tracking.last_alerted_price_cents == 80000


def test_a_recovery_above_the_target_re_arms_the_alert(now, hours):
    """The half of the rule everybody forgets.

    Without the reset, a product that dropped to 889, recovered to 1200 and fell back to
    950 would stay silent forever: 950 is not below the 889 last announced, even though
    from the user's point of view it just dropped below their target again.
    """
    tracking = make_tracking(target_price_cents=100000)
    tracking.register_price(88900, now=now)
    assert tracking.last_alerted_price_cents == 88900

    tracking.register_price(120000, now=now + hours(24))
    assert tracking.last_alerted_price_cents is None

    # 950 is not below the 889 already announced, so only the reset makes this an alert.
    assert tracking.register_price(95000, now=now + hours(48)) is True


def test_a_recovery_re_arms_even_inside_the_cooldown(now, hours):
    # The reset is not rate-limited; only the alerting is.
    tracking = make_tracking()
    tracking.register_price(88900, now=now)

    tracking.register_price(120000, now=now + hours(1))

    assert tracking.last_alerted_price_cents is None


def test_the_cooldown_boundary_is_inclusive(now):
    tracking = make_tracking()
    tracking.register_price(88900, now=now)

    assert tracking.register_price(80000, now=now + ALERT_COOLDOWN) is True


def test_should_alert_changes_nothing(now):
    tracking = make_tracking()

    assert tracking.should_alert(88900, now=now) is True
    assert tracking.last_alerted_price_cents is None
    assert tracking.last_alerted_at is None


async def test_the_alert_state_survives_a_round_trip(session, now):
    user = User(telegram_id=1)
    product = Product(
        store="cyberpuerta",
        external_id="abc",
        canonical_url="https://www.cyberpuerta.mx/a.html",
        name="SSD",
        currency="MXN",
    )
    session.add_all([user, product])
    await session.flush()

    tracking = Tracking(user_id=user.id, product_id=product.id, target_price_cents=90000)
    session.add(tracking)
    tracking.register_price(88900, now=now)
    await session.commit()

    session.expunge_all()
    reloaded = await session.scalar(select(Tracking))

    # The comparison the cooldown makes on the next run needs an aware datetime on both
    # sides; a naive one read back from SQLite would raise here rather than in a test.
    assert reloaded.last_alerted_at == now
    assert reloaded.should_alert(88900, now=now) is False


def test_the_tracking_model_exposes_what_phase_four_needs():
    # A cheap guard against the seam being renamed out from under the checker.
    assert {"should_alert", "register_price"} <= set(dir(Tracking))
    assert Tracking.__tablename__ == "trackings"
