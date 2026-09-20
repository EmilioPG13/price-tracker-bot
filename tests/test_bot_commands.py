"""Tests for the three commands, end to end and offline.

"End to end" here means the real parser reading a real captured page, the real
repository writing to a real database, and the real alert rule on the tracking row. The
only thing replaced is the network: `SavedPage` hands over a page from
`tests/fixtures/` instead of fetching one.

That is the whole reason `commands.py` imports no `telegram`. These exercise what `/add`
*does* — which rows appear, which alert is consumed, which request is never made — with
no `Update` built anywhere, and they would read the same if the bot moved to a different
chat platform tomorrow.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from price_tracker.bot import commands, copy
from price_tracker.bot.runtime import Resources
from price_tracker.db import (
    PriceHistory,
    Product,
    ProductStatus,
    Repository,
    Tracking,
    User,
    create_session_factory,
    session_scope,
)
from price_tracker.scrapers import (
    Fetcher,
    PageContent,
    StoreRefusedError,
    UnsupportedUrlError,
)

KINGSTON = "cyberpuerta-ssd-kingston-a400.html"
ACER = "cyberpuerta-ssd-acer-gm7.html"

KINGSTON_URL = "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"
KINGSTON_ID = "c156664ff9062de87fc3bf694dbb8eae"
KINGSTON_CENTS = 88900

ACER_URL = "https://www.cyberpuerta.mx/SSD-Acer-GM7-1TB.html"
ACER_ID = "494415ec3f92405919da43332b38a307"

# A store this bot does not read. Mercado Libre is deferred rather than dropped
# (`docs/store-viability.md`: the price is not in its server HTML at all), which makes it
# the honest example of a link a user will plausibly paste and get turned away. This
# used to be a Liverpool URL, until phase 5 made Liverpool supported.
UNSUPPORTED_URL = "https://articulo.mercadolibre.com.mx/MLM-656960312-licuadora-oster-_JM"

# Arbitrary. A real Telegram id is a personal identifier and would prove nothing here —
# these tests care that two ids are different, not what either one is.
A_USER = 11111111
ANOTHER_USER = 22222222


class SavedPage(Fetcher):
    """Serves a captured page, and remembers every URL it was asked for.

    `html` is writable so a test can change which product the store answers with,
    which is how two different products get added in one test.
    """

    def __init__(self, html: str) -> None:
        self.html = html
        self.requested: list[str] = []

    async def fetch(self, url: str) -> PageContent:
        self.requested.append(url)
        return PageContent(url=url, html=self.html)


class BrokenStore(Fetcher):
    """A store that fails the same way every time."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.requested: list[str] = []

    async def fetch(self, url: str) -> PageContent:
        self.requested.append(url)
        raise self.error


@pytest.fixture
def fetcher(load_fixture):
    return SavedPage(load_fixture(KINGSTON))


@pytest.fixture
def resources(engine, fetcher):
    """The application's resources, pointed at an in-memory database and a saved page.

    No `http_client`: there is nothing to close, and leaving it `None` is what proves
    `Resources` does not assume it owns one.
    """
    return Resources(engine=engine, sessions=create_session_factory(engine), fetcher=fetcher)


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


# ---- /add --------------------------------------------------------------------


async def test_add_stores_the_product_the_tracking_and_the_reading(resources, engine):
    reply = await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    assert "Kingston" in reply
    assert "$889.00 MXN" in reply  # the price the page carried
    assert "$800.00 MXN" in reply  # the target, echoed back

    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.store == "cyberpuerta"
        assert product.external_id == KINGSTON_ID
        assert product.last_price_cents == KINGSTON_CENTS
        assert product.last_checked_at is not None

        tracking = await session.scalar(select(Tracking))
        assert tracking.target_price_cents == 80000

        # `/add` read a page, so that reading is an observation like any other.
        entry = await session.scalar(select(PriceHistory))
        assert entry.price_cents == KINGSTON_CENTS
        assert entry.in_stock is True


async def test_the_target_goes_through_to_cents_not_through_float(resources):
    # "1,899.00" from a user has to become the same integer as "1899.00" from JSON-LD.
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "1,899.00"])

    async with session_scope(resources.sessions) as session:
        tracking = await session.scalar(select(Tracking))
        assert tracking.target_price_cents == 189900


async def test_the_url_is_normalised_before_the_request(resources, fetcher):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL + "?utm_source=telegram", "800"])

    # The tracking parameter never reaches the store, and one page is one request.
    assert fetcher.requested == [KINGSTON_URL]


async def test_adding_the_same_product_twice_moves_the_target_instead_of_duplicating(
    resources, engine
):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "750"])

    async with session_scope(resources.sessions) as session:
        assert await count(session, Product) == 1
        assert await count(session, Tracking) == 1
        # Both readings are kept: each one cost a request and happened at a real moment.
        assert await count(session, PriceHistory) == 2

        tracking = await session.scalar(select(Tracking))
        assert tracking.target_price_cents == 75000


async def test_two_users_tracking_one_product_share_the_row(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    await commands.add_tracking(resources, ANOTHER_USER, [KINGSTON_URL, "700"])

    async with session_scope(resources.sessions) as session:
        # One product, one request per check, two people told about it. This is the
        # reason `products` is not keyed by user.
        assert await count(session, Product) == 1
        assert await count(session, Tracking) == 2
        assert await count(session, User) == 2


async def test_a_price_already_under_target_is_alerted_by_the_reply_itself(resources):
    reply = await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "1000"])

    assert "🎉" in reply

    async with session_scope(resources.sessions) as session:
        tracking = await session.scalar(select(Tracking))
        # The alert was consumed here, so the phase 4 checker will not announce the same
        # price again six hours later. This is `register_price` doing its job.
        assert tracking.last_alerted_price_cents == KINGSTON_CENTS
        assert tracking.last_alerted_at is not None


async def test_a_price_above_target_leaves_the_alert_armed(resources):
    reply = await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "500"])

    assert "🎉" not in reply

    async with session_scope(resources.sessions) as session:
        tracking = await session.scalar(select(Tracking))
        assert tracking.last_alerted_price_cents is None
        assert tracking.last_alerted_at is None


@pytest.mark.parametrize(
    "args",
    [
        [],
        [KINGSTON_URL],
        [KINGSTON_URL, "800", "pesos"],
    ],
    ids=["nothing", "url-only", "too-many"],
)
async def test_add_needs_exactly_a_link_and_a_price(resources, fetcher, args):
    reply = await commands.add_tracking(resources, A_USER, args)

    assert reply == copy.ADD_USAGE
    assert fetcher.requested == []  # nothing is fetched on a usage error


@pytest.mark.parametrize("raw", ["gratis", "1.899,00", "-10", ""])
async def test_a_price_that_cannot_be_read_exactly_is_refused(resources, fetcher, raw):
    reply = await commands.add_tracking(resources, A_USER, [KINGSTON_URL, raw])

    assert reply == copy.bad_price(raw)
    # Checked before the network: a typo should not cost the store a request.
    assert fetcher.requested == []


async def test_the_bad_price_message_quotes_what_was_typed(resources):
    # `/add 800 <link>` is the commonest mistake, and quoting the text makes the swapped
    # arguments visible without the message having to explain them.
    reply = await commands.add_tracking(resources, A_USER, ["800", KINGSTON_URL])

    assert KINGSTON_URL in reply


async def test_a_target_of_zero_is_refused(resources):
    assert await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "0"]) == copy.ZERO_TARGET


async def test_an_unsupported_store_is_refused_without_a_request(resources, fetcher):
    reply = await commands.add_tracking(resources, A_USER, [UNSUPPORTED_URL, "800"])

    assert reply == copy.reply_for_error(UnsupportedUrlError())
    # The conduct rule, as code: a store we cannot read is one we do not knock on.
    assert fetcher.requested == []


async def test_a_store_refusal_is_reported_and_writes_nothing(engine):
    resources = Resources(
        engine=engine,
        sessions=create_session_factory(engine),
        fetcher=BrokenStore(StoreRefusedError("HTTP 403")),
    )

    reply = await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    assert reply == copy.reply_for_error(StoreRefusedError())
    async with session_scope(resources.sessions) as session:
        # A failed read is not a tracking. The user asked for something that did not
        # happen, and a half-written row would show up in `/list` as if it had.
        assert await count(session, Product) == 0
        assert await count(session, Tracking) == 0


# ---- /list -------------------------------------------------------------------


async def test_list_says_so_when_there_is_nothing(resources):
    assert await commands.list_trackings(resources, A_USER) == copy.LIST_EMPTY


async def test_list_shows_the_current_price_the_target_and_the_link(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    reply = await commands.list_trackings(resources, A_USER)

    assert copy.LIST_HEADER in reply
    assert "1. " in reply
    assert "Kingston" in reply
    assert "$889.00 MXN" in reply  # last read
    assert "$800.00 MXN" in reply  # target
    # The store's own canonical URL, which is a third spelling of neither the pasted
    # link nor the one that was requested. See `docs/scraper-design.md`.
    assert "cyberpuerta.mx" in reply


async def test_list_shows_two_products_numbered_for_remove(resources, fetcher, load_fixture):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    fetcher.html = load_fixture(ACER)
    await commands.add_tracking(resources, A_USER, [ACER_URL, "3000"])

    reply = await commands.list_trackings(resources, A_USER)

    assert "1. " in reply
    assert "2. " in reply
    assert "Kingston" in reply
    assert "Acer" in reply


async def test_list_shows_only_your_own_trackings(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    assert await commands.list_trackings(resources, ANOTHER_USER) == copy.LIST_EMPTY


async def test_a_never_checked_product_says_so_rather_than_showing_a_price(resources):
    # Reachable from a data migration or a future `/add` that defers the first read;
    # `last_price_cents` is nullable precisely to tell "never read" from "free".
    async with session_scope(resources.sessions) as session:
        repo = Repository(session)
        user = await repo.get_or_create_user(A_USER)
        product = Product(
            store="cyberpuerta",
            external_id="deadbeef",
            canonical_url="https://www.cyberpuerta.mx/x.html",
            name="Algo que nadie ha leído",
            currency="MXN",
        )
        session.add(product)
        await session.flush()
        await repo.set_tracking(user, product, 50000)

    reply = await commands.list_trackings(resources, A_USER)

    assert "sin leer todavía" in reply


async def test_a_retired_product_is_marked_as_paused(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        product.status = ProductStatus.RETIRED

    reply = await commands.list_trackings(resources, A_USER)

    # Otherwise a product the checker gave up on looks exactly like one whose price has
    # simply not moved, and the user waits for an alert that is never coming.
    assert "en pausa" in reply


# ---- /remove -----------------------------------------------------------------


async def test_remove_takes_the_number_from_list_and_names_what_went(
    resources, fetcher, load_fixture
):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    fetcher.html = load_fixture(ACER)
    await commands.add_tracking(resources, A_USER, [ACER_URL, "3000"])

    reply = await commands.remove_tracking(resources, A_USER, ["1"])

    # The confirmation names the product, because the user chose it by position and a
    # position is only as good as the listing they were looking at.
    assert "Kingston" in reply

    remaining = await commands.list_trackings(resources, A_USER)
    assert "Kingston" not in remaining
    assert "Acer" in remaining


async def test_remove_keeps_the_product_and_its_history(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    await commands.remove_tracking(resources, A_USER, ["1"])

    async with session_scope(resources.sessions) as session:
        assert await count(session, Tracking) == 0
        # Someone else may be watching it, and re-adding it later should not start from
        # an empty chart. History is cheap; a discarded price series is not recoverable.
        assert await count(session, Product) == 1
        assert await count(session, PriceHistory) == 1


@pytest.mark.parametrize("args", [[], ["dos"], ["1", "2"], ["1.0"]])
async def test_remove_needs_one_number(resources, args):
    assert await commands.remove_tracking(resources, A_USER, args) == copy.REMOVE_USAGE


@pytest.mark.parametrize("position", ["0", "2", "-1", "999"])
async def test_a_number_that_points_at_nothing_is_reported(resources, position):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    reply = await commands.remove_tracking(resources, A_USER, [position])

    assert reply == copy.REMOVE_NOT_FOUND
    async with session_scope(resources.sessions) as session:
        assert await count(session, Tracking) == 1


async def test_removing_from_an_empty_list_is_not_an_error(resources):
    assert await commands.remove_tracking(resources, A_USER, ["1"]) == copy.REMOVE_NOT_FOUND


async def test_you_cannot_remove_someone_else_s_tracking(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    # Position 1 exists — for the other user. The list is resolved per user, so this
    # finds nothing rather than reaching into a stranger's row.
    reply = await commands.remove_tracking(resources, ANOTHER_USER, ["1"])

    assert reply == copy.REMOVE_NOT_FOUND
    async with session_scope(resources.sessions) as session:
        assert await count(session, Tracking) == 1
