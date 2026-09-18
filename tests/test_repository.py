"""Tests for the seam the application talks to.

The two runtimes — the `/add` handler and the phase 4 checker — share this class so
they cannot drift. What is worth testing here is therefore the behaviour a handler
would otherwise reimplement slightly differently: what happens when the same product
arrives twice, what the checker considers due, and what a failure costs a product.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from price_tracker.db import MAX_CONSECUTIVE_FAILURES, Product, ProductStatus, Repository
from price_tracker.scrapers import Fetcher, PageContent, fetch_product

SIX_HOURS = timedelta(hours=6)

# The same SSD, as it actually reaches us. The first is what a category page links to,
# the second is that link with Telegram's tracking parameter on it, and the third is
# OXID's internal address — the form the page itself publishes. `docs/scraper-design.md`
# has the capture showing the store answering all three.
CATEGORY_URL = (
    "https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/"
    "SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"
)
SHARED_URL = CATEGORY_URL + "?utm_source=telegram&campaign=x"
OXID_URL = "https://www.cyberpuerta.mx/index.php?cl=details&anid=c156664ff9062de87fc3bf694dbb8eae"


class SavedPageFetcher(Fetcher):
    """Serves one saved page whatever it is asked for, and remembers the asking."""

    def __init__(self, html: str) -> None:
        self.html = html
        self.requested: list[str] = []

    async def fetch(self, url: str) -> PageContent:
        self.requested.append(url)
        return PageContent(url=url, html=self.html)


@pytest.fixture
def fetcher(load_fixture):
    return SavedPageFetcher(load_fixture("cyberpuerta-ssd-kingston-a400.html"))


# ---- identity ------------------------------------------------------------------


@pytest.mark.parametrize("pasted", [CATEGORY_URL, SHARED_URL, OXID_URL])
async def test_every_spelling_of_one_url_reaches_one_row(repo, session, fetcher, pasted):
    """The rule the whole identity design exists for, proved end to end.

    The product is added once through the category URL, then again through `pasted`.
    The two go to *different* addresses — the tracking parameters are stripped, and the
    OXID form keeps a query the SEO form does not have — and still converge, because the
    key is `(store, external_id)` and `external_id` is read from the page, never from
    the link. A URL-keyed table would hold three rows for one SSD.
    """
    first = await repo.upsert_product(await fetch_product(CATEGORY_URL, fetcher))
    second = await repo.upsert_product(await fetch_product(pasted, fetcher))
    await session.commit()

    assert second.id == first.id
    assert len((await session.scalars(select(Product))).all()) == 1


async def test_a_renamed_slug_updates_the_link_and_not_the_identity(repo, session, product_data):
    original = await repo.upsert_product(product_data())
    await session.commit()

    renamed = await repo.upsert_product(
        product_data(
            canonical_url="https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-2-5-SATA-III.html",
            name="SSD Kingston A400 240GB 2.5 SATA III 500 MB/s Lectura",
        )
    )

    assert renamed.id == original.id
    assert renamed.canonical_url.endswith("SATA-III.html")
    assert "Lectura" in renamed.name


async def test_a_new_product_is_created_once_per_store(repo, session, product_data):
    await repo.upsert_product(product_data())
    await repo.upsert_product(product_data(store="liverpool"))
    await session.commit()

    assert len((await session.scalars(select(Product))).all()) == 2


# ---- users and trackings -------------------------------------------------------


async def test_a_user_is_created_on_first_contact_and_found_after(repo, session):
    first = await repo.get_or_create_user(7_284_119_365)
    await session.commit()

    again = await repo.get_or_create_user(7_284_119_365)

    assert again.id == first.id


async def test_tracking_the_same_product_twice_moves_the_target(repo, session, product_data):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())

    first = await repo.set_tracking(user, product, 80000)
    second = await repo.set_tracking(user, product, 75000)
    await session.commit()

    assert second.id == first.id
    assert second.target_price_cents == 75000
    assert len(await repo.list_trackings(user)) == 1


async def test_lowering_the_target_re_arms_the_alert(repo, session, product_data, now):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())
    tracking = await repo.set_tracking(user, product, 90000)
    tracking.register_price(88900, now=now)
    await session.commit()

    # The user is asking to hear about it again at a lower price. Leaving the alert
    # state alone would silence exactly the drop they just asked for.
    await repo.set_tracking(user, product, 70000)

    assert tracking.last_alerted_price_cents is None


async def test_re_setting_the_same_target_does_not_re_arm(repo, session, product_data, now):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())
    tracking = await repo.set_tracking(user, product, 90000)
    tracking.register_price(88900, now=now)
    await session.commit()

    await repo.set_tracking(user, product, 90000)

    assert tracking.last_alerted_price_cents == 88900


async def test_listing_trackings_loads_the_product(repo, session, product_data):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())
    await repo.set_tracking(user, product, 80000)
    await session.commit()
    session.expunge_all()

    user = await repo.get_or_create_user(1)
    [tracking] = await repo.list_trackings(user)

    # Reaching through to the product is what `/list` does. Without the selectinload
    # this raises, because the relationship is declared `lazy="raise_on_sql"`.
    assert tracking.product.name == "SSD Kingston A400 240GB"


async def test_the_checker_can_find_everyone_watching_a_product(repo, session, product_data):
    product = await repo.upsert_product(product_data())
    for telegram_id in (1, 2, 3):
        user = await repo.get_or_create_user(telegram_id)
        await repo.set_tracking(user, product, 80000)
    await session.commit()
    session.expunge_all()

    watchers = await repo.trackings_for_product(product.id)

    assert len(watchers) == 3
    assert {tracking.user.telegram_id for tracking in watchers} == {1, 2, 3}


async def test_removing_a_tracking_keeps_the_product(repo, session, product_data):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())
    await repo.set_tracking(user, product, 80000)
    await session.commit()

    removed = await repo.remove_tracking(user, product.id)
    await session.commit()

    assert removed is True
    assert await repo.list_trackings(user) == []
    # Somebody else may be watching it, and the history is worth keeping regardless.
    assert await session.scalar(select(Product.id)) == product.id


async def test_removing_a_tracking_that_is_not_there_says_so(repo, session, product_data):
    user = await repo.get_or_create_user(1)
    product = await repo.upsert_product(product_data())
    await session.commit()

    assert await repo.remove_tracking(user, product.id) is False


# ---- the checker's view --------------------------------------------------------


async def test_a_never_checked_product_is_due(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())
    await session.commit()

    due = await repo.products_due_for_check(interval=SIX_HOURS, now=now)

    assert [p.id for p in due] == [product.id]


async def test_a_recently_checked_product_is_not_due(repo, session, product_data, now, hours):
    product = await repo.upsert_product(product_data())
    await repo.record_success(product, product_data(), now=now)
    await session.commit()

    assert await repo.products_due_for_check(interval=SIX_HOURS, now=now + hours(1)) == []
    assert len(await repo.products_due_for_check(interval=SIX_HOURS, now=now + hours(7))) == 1


async def test_never_checked_products_come_first(repo, session, product_data, now, hours):
    """The two backends disagree about where NULL sorts, so this is asserted explicitly.

    A checker that silently visits brand-new products last is not an error anyone would
    notice from a log.
    """
    checked = await repo.upsert_product(product_data(external_id="checked"))
    await repo.record_success(checked, product_data(external_id="checked"), now=now - hours(48))
    fresh = await repo.upsert_product(product_data(external_id="fresh"))
    await session.commit()

    due = await repo.products_due_for_check(interval=SIX_HOURS, now=now)

    assert [p.id for p in due] == [fresh.id, checked.id]


async def test_a_retired_product_is_never_due(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())
    await repo.record_failure(product, permanent=True, now=now)
    await session.commit()

    assert await repo.products_due_for_check(interval=SIX_HOURS, now=now) == []


async def test_the_checker_can_cap_how_much_it_takes_on(repo, session, product_data, now):
    for index in range(5):
        await repo.upsert_product(product_data(external_id=f"id{index}"))
    await session.commit()

    due = await repo.products_due_for_check(interval=SIX_HOURS, limit=2, now=now)

    assert len(due) == 2


# ---- recording a check ---------------------------------------------------------


async def test_a_successful_check_appends_history_and_refreshes_the_product(
    repo, session, product_data, now
):
    product = await repo.upsert_product(product_data())
    await repo.record_success(product, product_data(price_cents=79900), now=now)
    await session.commit()

    assert product.last_price_cents == 79900
    assert product.last_checked_at == now

    [entry] = await repo.price_history(product.id)
    assert entry.price_cents == 79900
    assert entry.in_stock is True


async def test_out_of_stock_is_recorded_as_data_not_dropped(repo, session, product_data, now):
    """Phase 1 settled that an out-of-stock product keeps its price and flips a flag.

    Recording the price without the flag would draw a flat line through a period when
    the thing could not actually be bought.
    """
    product = await repo.upsert_product(product_data())
    await repo.record_success(product, product_data(in_stock=False), now=now)
    await session.commit()

    [entry] = await repo.price_history(product.id)
    assert entry.in_stock is False
    assert entry.price_cents == 88900


async def test_history_accumulates_in_order(repo, session, product_data, now, hours):
    product = await repo.upsert_product(product_data())
    for index, price in enumerate((88900, 85000, 79900)):
        await repo.record_success(
            product, product_data(price_cents=price), now=now + hours(index * 6)
        )
    await session.commit()

    assert [e.price_cents for e in await repo.price_history(product.id)] == [88900, 85000, 79900]


async def test_history_can_be_windowed(repo, session, product_data, now, hours):
    product = await repo.upsert_product(product_data())
    await repo.record_success(product, product_data(price_cents=88900), now=now - hours(48))
    await repo.record_success(product, product_data(price_cents=79900), now=now - hours(2))
    await session.commit()

    recent = await repo.price_history(product.id, since=now - hours(24))

    assert [e.price_cents for e in recent] == [79900]


async def test_a_success_clears_a_bad_week(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())
    await repo.record_failure(product, now=now)
    await repo.record_failure(product, now=now)

    await repo.record_success(product, product_data(), now=now)

    # A product that answered is healthy, however badly last week went.
    assert product.consecutive_failures == 0
    assert product.status is ProductStatus.ACTIVE


async def test_one_bad_afternoon_does_not_retire_a_product(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())

    retired = await repo.record_failure(product, now=now)

    assert retired is False
    assert product.status is ProductStatus.ACTIVE
    # The scheduling field moves on failure too, or a failing product would come up
    # again on every single run.
    assert product.last_checked_at == now


async def test_enough_failures_in_a_row_retires_a_product(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        assert await repo.record_failure(product, now=now) is False

    assert await repo.record_failure(product, now=now) is True
    assert product.status is ProductStatus.RETIRED


async def test_a_page_that_is_gone_retires_immediately(repo, session, product_data, now):
    # A 404 does not get better by waiting, so there is no reason to spend five more
    # requests proving it. The caller passes permanent=True on a PageGoneError.
    product = await repo.upsert_product(product_data())

    assert await repo.record_failure(product, permanent=True, now=now) is True
    assert product.consecutive_failures == 1


async def test_pasting_a_retired_product_again_revives_it(repo, session, product_data, now):
    product = await repo.upsert_product(product_data())
    await repo.record_failure(product, permanent=True, now=now)
    await session.commit()

    revived = await repo.upsert_product(product_data())

    # It parsed, so whatever retired it is over.
    assert revived.id == product.id
    assert revived.status is ProductStatus.ACTIVE
    assert revived.consecutive_failures == 0


async def test_the_repository_is_the_only_thing_the_checker_needs(repo):
    # Phase 4's check_all_prices() is a loop over these three and nothing else. If it
    # ever needs a select() of its own, that query belongs here instead.
    for name in ("products_due_for_check", "record_success", "record_failure"):
        assert callable(getattr(repo, name))
    assert isinstance(repo, Repository)
