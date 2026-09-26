"""Tests for the scheduled checker: what it reads, what it records, who it tells.

Same discipline as `test_bot_commands.py`, from the other runtime. The real parser reads
a real captured page, the real repository writes to a real database, and the real alert
rule decides who hears about it. Two things are replaced: the network, by a stub store
that serves the capture with a price of the test's choosing, and Telegram, by a list.

**The price is rewritten in the captured HTML rather than mocked further up.** The whole
value of these tests is that a reading travels the same path it travels in production —
through `fetch_product`, through the JSON-LD extractor, through `to_cents` — so the
number the store quotes is the one thing worth varying, and it is varied where a store
would vary it. `"price":889` appears exactly once in the capture, which is what makes
the substitution safe rather than clever.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from price_tracker.bot import checker, commands
from price_tracker.bot.runtime import Resources
from price_tracker.db import (
    MAX_CONSECUTIVE_FAILURES,
    PriceHistory,
    Product,
    ProductStatus,
    Tracking,
    create_session_factory,
    session_scope,
    utc_now,
)
from price_tracker.scrapers import (
    Fetcher,
    LayoutChangedError,
    PageContent,
    PageGoneError,
    StoreRefusedError,
    StoreUnavailableError,
)

KINGSTON = "cyberpuerta-ssd-kingston-a400.html"
KINGSTON_URL = "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"
KINGSTON_CENTS = 88900

ACER = "cyberpuerta-ssd-acer-gm7.html"
ACER_URL = "https://www.cyberpuerta.mx/SSD-Acer-GM7-1TB.html"

A_USER = 11111111
ANOTHER_USER = 22222222

#: The schedule's period, as `jobs.py` passes it. The checker derives what is due from
#: it, so the tests pass the same number and never a threshold of their own.
INTERVAL = timedelta(hours=6)

#: No waiting. The retry policy is asserted by counting attempts, not by timing them —
#: a test that actually slept through `RETRY_BACKOFF` would take 35 seconds to prove
#: something a counter proves instantly.
NO_WAITING: tuple[float, ...] = ()


def at_price(html: str, cents: int, *, in_stock: bool = True) -> str:
    """The captured page, re-quoted at a different price."""
    pesos = cents // 100 if cents % 100 == 0 else cents / 100
    page = html.replace('"price":889', f'"price":{pesos}')
    if not in_stock:
        page = page.replace('"availability":"InStock"', '"availability":"OutOfStock"')
    return page


class StoreStub(Fetcher):
    """A store that answers whatever the test queued, and counts what it was asked.

    `responses` is consumed in order; the last one repeats forever, which is what makes
    "a store that is down" and "a store that is down once" both easy to express. A
    response is either HTML to serve or an exception to raise.
    """

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.requested: list[str] = []

    async def fetch(self, url: str) -> PageContent:
        self.requested.append(url)
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return PageContent(url=url, html=response)


class Outbox:
    """Telegram, as the checker sees it: somewhere to put a message."""

    def __init__(self, *, failing: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failing = failing

    async def __call__(self, telegram_id: int, text: str) -> None:
        if self.failing:
            raise RuntimeError("Telegram is having a day")
        self.sent.append((telegram_id, text))


class Clock:
    """`utc_now` as the repository sees it, moved by the test rather than by time."""

    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now


class SlowStore(StoreStub):
    """A store that takes four seconds to answer, on the test's clock."""

    def __init__(self, clock: Clock, *responses: str | Exception) -> None:
        super().__init__(*responses)
        self.clock = clock

    async def fetch(self, url: str) -> PageContent:
        self.clock.now += timedelta(seconds=4)
        return await super().fetch(url)


@pytest.fixture
def kingston(load_fixture):
    return load_fixture(KINGSTON)


@pytest.fixture
def outbox():
    return Outbox()


def resources_with(engine, fetcher: Fetcher) -> Resources:
    return Resources(engine=engine, sessions=create_session_factory(engine), fetcher=fetcher)


async def tracked(engine, kingston: str, *, target: str, telegram_id: int = A_USER) -> Resources:
    """Get a product into the database the way a user would: by running `/add`.

    Deliberately not by inserting rows. `/add` records a reading and consumes the first
    alert, and those are exactly the preconditions the checker has to cope with — a
    fixture that built the rows by hand would test the checker against a state the
    application never actually produces.
    """
    resources = resources_with(engine, StoreStub(kingston))
    await commands.add_tracking(resources, telegram_id, [KINGSTON_URL, target])
    return resources


async def add_second_product(resources: Resources, html: str) -> None:
    """A second tracked product, so a run has more than one thing to walk.

    Its target is deliberately unreachable: these tests are about the loop, and an
    incidental alert from the second product would muddy what they assert.
    """
    original = resources.fetcher
    resources.fetcher = StoreStub(html)
    await commands.add_tracking(resources, A_USER, [ACER_URL, "1"])
    resources.fetcher = original


async def make_due(resources: Resources) -> None:
    """Backdate every product so the next run considers it. Cheaper than waiting."""
    async with session_scope(resources.sessions) as session:
        for product in (await session.scalars(select(Product))).all():
            product.last_checked_at = utc_now() - timedelta(days=1)


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


# ---- what gets checked -------------------------------------------------------


async def test_a_due_product_is_read_and_recorded(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 79900))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert (run.checked, run.succeeded, run.failed) == (1, 1, 0)
    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.last_price_cents == 79900
        # `/add` wrote the first observation; this is the second.
        assert await count(session, PriceHistory) == 2


async def test_a_product_checked_recently_is_left_alone(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    # No backdating: `/add` set `last_checked_at` a moment ago.
    store = StoreStub(kingston)
    resources.fetcher = store

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 0
    # The point is the request that was never made. The interval is the rate limiter,
    # so a bot restarting every few minutes costs the store nothing.
    assert store.requested == []


async def test_what_one_pass_read_is_due_at_the_next(engine, kingston, outbox, monkeypatch):
    """The schedule and the due-query have to agree about exactly one period later.

    A pass stamps each product when its page arrives, a few seconds after the pass
    began, and the next pass begins one period after this one did — so every product is
    those seconds short of a full period. A checker that waited for a full one skipped
    every other pass: in production a six-hour check ran every twelve hours, and nothing
    failed. Asking at one hour and at seven, as the repository tests do, cannot see it.
    """
    clock = Clock()
    monkeypatch.setattr("price_tracker.db.repository.utc_now", clock)
    resources = await tracked(engine, kingston, target="800")
    store = SlowStore(clock, kingston)
    resources.fetcher = store

    first_pass = clock.now + INTERVAL
    clock.now = first_pass
    await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)
    clock.now = first_pass + INTERVAL
    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 1
    assert len(store.requested) == 2


async def test_the_batch_limit_caps_one_pass(engine, kingston, load_fixture, outbox):
    resources = await tracked(engine, kingston, target="800")
    await add_second_product(resources, load_fixture(ACER))
    await make_due(resources)
    resources.fetcher = StoreStub(kingston)

    run = await checker.check_all_prices(
        resources, outbox, interval=INTERVAL, limit=1, backoff=NO_WAITING
    )

    assert run.checked == 1


async def test_an_out_of_stock_reading_is_data_not_a_failure(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 79900, in_stock=False))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert (run.succeeded, run.failed) == (1, 0)
    async with session_scope(resources.sessions) as session:
        entry = (await session.scalars(select(PriceHistory).order_by(PriceHistory.id))).all()[-1]
        assert entry.in_stock is False
        assert entry.price_cents == 79900
    # And it still alerts: something agotado may come back at that price.
    assert len(outbox.sent) == 1
    assert "agotado" in outbox.sent[0][1]


# ---- alerts ------------------------------------------------------------------


async def test_a_drop_past_the_target_alerts_the_user_who_asked(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 79900))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.alerted == 1
    telegram_id, text = outbox.sent[0]
    assert telegram_id == A_USER
    assert "$799.00 MXN" in text  # the new price
    assert "$889.00 MXN" in text  # what it cost before, which only exists at this moment
    assert "$800.00 MXN" in text  # the target, so the message stands on its own
    assert "Bajó" in text


async def test_a_price_above_the_target_tells_nobody(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 85000))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.succeeded == 1
    assert outbox.sent == []


async def test_the_alert_add_already_sent_is_not_repeated(engine, kingston, outbox):
    """The phase 3 decision, enforced from the phase 4 side.

    `/add` on a product already under target replies with the alert and consumes it
    through `register_price`. If the checker did not go through the same door, the user
    would be told the same thing again a few hours later.
    """
    resources = await tracked(engine, kingston, target="900")  # 889 is already under it
    await make_due(resources)
    resources.fetcher = StoreStub(kingston)  # same price, unchanged

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.succeeded == 1
    assert outbox.sent == []


async def test_two_users_watching_one_product_are_told_separately(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await commands.add_tracking(resources, ANOTHER_USER, [KINGSTON_URL, "850"])
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 79900))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    # One request, two messages. That is why `Product` is shared rather than per-user.
    assert len(resources.fetcher.requested) == 1
    assert run.alerted == 2
    assert {telegram_id for telegram_id, _ in outbox.sent} == {A_USER, ANOTHER_USER}


async def test_a_failed_delivery_is_not_counted_as_an_alert(engine, kingston):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(at_price(kingston, 79900))

    run = await checker.check_all_prices(
        resources, Outbox(failing=True), interval=INTERVAL, backoff=NO_WAITING
    )

    # The reading still lands; only the message was lost.
    assert (run.succeeded, run.alerted) == (1, 0)
    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.last_price_cents == 79900


async def test_the_cooldown_suppresses_later_drops_and_a_rise_re_arms(engine, kingston, outbox):
    """The whole alert rule, walked in order, through the checker.

    889 -> 799 alerts. 799 -> 780 does not, because the cooldown has not elapsed. A rise
    to 950 clears the armed price — that is the half everybody forgets, and it is what
    makes the *next* drop a new event rather than a worse version of the old one. 795
    then stays quiet only because it is still inside the same 12-hour window.
    """
    resources = await tracked(engine, kingston, target="800")

    for price in (79900, 78000, 95000, 79500):
        await make_due(resources)
        resources.fetcher = StoreStub(at_price(kingston, price))
        await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    # 799 alerted. 780 was suppressed by the 12-hour cooldown. 950 is above target and
    # only re-armed. 795 is the next real event — but it is still inside the cooldown
    # window, so it waits. The rule is deliberately quiet.
    assert len(outbox.sent) == 1
    assert "$799.00 MXN" in outbox.sent[0][1]

    async with session_scope(resources.sessions) as session:
        tracking = await session.scalar(select(Tracking))
        # Re-armed by the rise and not re-set by 795, because 795 never alerted.
        assert tracking.last_alerted_price_cents is None
        assert await count(session, PriceHistory) == 5  # /add plus four checks


# ---- failure, retry and retirement -------------------------------------------


async def test_a_5xx_is_retried_and_a_recovery_counts(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    store = StoreStub(
        StoreUnavailableError("store is unavailable (HTTP 503)"),
        at_price(kingston, 79900),
    )
    resources.fetcher = store

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=(0.0,))

    assert len(store.requested) == 2
    assert (run.succeeded, run.failed) == (1, 0)
    assert run.alerted == 1


async def test_a_refusal_is_never_retried(engine, kingston, outbox):
    """A 403 is a finding to record, not an obstacle to work around.

    This is the conduct rule from `docs/store-viability.md` as an assertion: the count
    of requests is the promise. Retrying a refusal is the first step onto the road that
    ends in rotating proxies.
    """
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    store = StoreStub(StoreRefusedError("store refused with HTTP 403"))
    resources.fetcher = store

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=(0.0, 0.0))

    assert len(store.requested) == 1
    assert (run.succeeded, run.failed) == (0, 1)


async def test_a_timeout_is_not_retried_either(engine, kingston, outbox):
    from price_tracker.scrapers import FetchTimeoutError

    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    store = StoreStub(FetchTimeoutError("timed out"))
    resources.fetcher = store

    await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=(0.0, 0.0))

    # Only a 5xx is the store telling us it is broken. Everything else waits for the
    # next run, which is six hours away and is already the retry.
    assert len(store.requested) == 1


async def test_a_dead_page_retires_the_product_at_once(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(PageGoneError("page is gone (HTTP 404)"))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.retired == 1
    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.status is ProductStatus.RETIRED
        # The tracking and the history survive the product being retired.
        assert await count(session, Tracking) == 1
        assert await count(session, PriceHistory) == 1


async def test_a_retired_product_stops_costing_requests(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(PageGoneError("page is gone (HTTP 404)"))
    await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    await make_due(resources)
    store = StoreStub(kingston)
    resources.fetcher = store
    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 0
    assert store.requested == []


async def test_a_product_nobody_tracks_stops_costing_requests(engine, kingston, outbox):
    # `/remove` keeps the product and its history on purpose. Without this, every removed
    # product would be read every pass forever, and on a public bot the per-user cap could
    # be walked around by adding and removing until the passes were full.
    resources = await tracked(engine, kingston, target="800")
    await commands.remove_tracking(resources, A_USER, ["1"])
    await make_due(resources)
    store = StoreStub(kingston)
    resources.fetcher = store

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 0
    assert store.requested == []


async def test_transient_failures_retire_only_after_a_run_of_them(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")
    resources.fetcher = StoreStub(StoreRefusedError("store refused with HTTP 403"))

    for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
        await make_due(resources)
        run = await checker.check_all_prices(
            resources, outbox, interval=INTERVAL, backoff=NO_WAITING
        )
        # One bad afternoon at the store must not delete somebody's tracking.
        assert run.retired == (1 if attempt == MAX_CONSECUTIVE_FAILURES else 0)

    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.status is ProductStatus.RETIRED


async def test_a_success_forgives_the_failures_before_it(engine, kingston, outbox):
    resources = await tracked(engine, kingston, target="800")

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        await make_due(resources)
        resources.fetcher = StoreStub(StoreRefusedError("store refused with HTTP 403"))
        await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    await make_due(resources)
    resources.fetcher = StoreStub(kingston)
    await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        assert product.consecutive_failures == 0
        assert product.status is ProductStatus.ACTIVE


async def test_a_failed_check_still_moves_the_product_out_of_the_queue(engine, kingston, outbox):
    """`last_checked_at` is a scheduling field, not a record of success.

    Left alone on failure, a broken product would be due again on the very next pass and
    the checker would spend every run on it.
    """
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(StoreRefusedError("store refused with HTTP 403"))
    await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 0


async def test_one_broken_product_does_not_discard_another_reading(
    engine, kingston, load_fixture, outbox
):
    """The reason each product gets its own `session_scope`.

    Two products, the second unreadable. The first one's price has to survive, because a
    run that threw away everything whenever the last product failed would lose work in
    proportion to how long it ran.
    """
    resources = await tracked(engine, kingston, target="800")
    await add_second_product(resources, load_fixture(ACER))
    await make_due(resources)

    # The first product answers, the second is gone.
    resources.fetcher = StoreStub(at_price(kingston, 79900), PageGoneError("page is gone"))
    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert (run.checked, run.succeeded, run.failed) == (2, 1, 1)
    async with session_scope(resources.sessions) as session:
        prices = (await session.scalars(select(Product.last_price_cents))).all()
        assert 79900 in prices


async def test_a_layout_change_is_a_failure_like_any_other(engine, kingston, outbox):
    """It logs differently — it is the only error meaning this repo is out of date — but
    it must not be treated as a reading, and it must not stop the run."""
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)
    resources.fetcher = StoreStub(LayoutChangedError("no price in the page"))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert (run.failed, run.crashed) == (1, 0)
    async with session_scope(resources.sessions) as session:
        assert await count(session, PriceHistory) == 1  # only `/add`'s


async def test_an_unexpected_exception_costs_one_product_not_the_run(
    engine, kingston, load_fixture, outbox
):
    """Nobody is watching a scheduled job, so a crash halfway must not silently skip the
    rest. Being last in the list is not a reason to go unchecked."""
    resources = await tracked(engine, kingston, target="800")
    await add_second_product(resources, load_fixture(ACER))
    await make_due(resources)

    # Not a `ScraperError`: this stands in for a bug, not for the store misbehaving.
    resources.fetcher = StoreStub(MemoryError("something nobody planned for"), kingston)
    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.crashed == 1
    assert run.succeeded == 1  # the other product was still checked


# ---- the shape of a run ------------------------------------------------------


async def test_an_empty_database_does_nothing_quietly(engine, outbox):
    resources = resources_with(engine, StoreStub("<html></html>"))

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert run.checked == 0
    assert run.summary().startswith("checked=0")


async def test_a_product_deleted_mid_run_is_dropped_not_crashed(engine, kingston, outbox):
    """The gap the design opens on purpose: the due list is read, the session closes,
    and the row is loaded again afterwards. Between the two it may be gone."""
    resources = await tracked(engine, kingston, target="800")
    await make_due(resources)

    vanishing = StoreStub(at_price(kingston, 79900))
    original_fetch = vanishing.fetch

    async def fetch_then_delete(url: str):
        page = await original_fetch(url)
        async with session_scope(resources.sessions) as session:
            for product in (await session.scalars(select(Product))).all():
                await session.delete(product)
        return page

    vanishing.fetch = fetch_then_delete  # type: ignore[method-assign]
    resources.fetcher = vanishing

    run = await checker.check_all_prices(resources, outbox, interval=INTERVAL, backoff=NO_WAITING)

    assert (run.checked, run.succeeded, run.crashed) == (1, 0, 0)
    assert outbox.sent == []
