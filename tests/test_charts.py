"""Tests for the chart: the drawing, and the command that decides whether to draw.

A picture is mostly not assertable, and pretending otherwise produces tests that break
on a font change and never catch a wrong chart. So these check the things that are
actually decidable — that a PNG comes out, that the rendering touched no global state,
that the command refuses to draw what would be misleading — and leave whether it *looks*
right to a person looking at one.

The sharpest test here is `test_two_charts_render_at_once_without_interfering`, because
the bug it guards against does not raise. Two concurrent `/chart` commands sharing a
`pyplot` figure would each send a picture; they would just be the wrong ones.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from price_tracker.bot import charts, commands, copy
from price_tracker.bot.runtime import Resources
from price_tracker.db import (
    PriceHistory,
    Product,
    create_session_factory,
    session_scope,
    utc_now,
)
from price_tracker.scrapers import Fetcher, PageContent

KINGSTON = "cyberpuerta-ssd-kingston-a400.html"
KINGSTON_URL = "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"

A_USER = 11111111

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SavedPage(Fetcher):
    def __init__(self, html: str) -> None:
        self.html = html

    async def fetch(self, url: str) -> PageContent:
        return PageContent(url=url, html=self.html)


def points(*prices: int, in_stock: bool = True) -> list[charts.PricePoint]:
    start = utc_now() - timedelta(days=len(prices))
    return [
        charts.PricePoint(start + timedelta(days=offset), price, in_stock)
        for offset, price in enumerate(prices)
    ]


@pytest.fixture
def resources(engine, load_fixture):
    return Resources(
        engine=engine,
        sessions=create_session_factory(engine),
        fetcher=SavedPage(load_fixture(KINGSTON)),
    )


# ---- the drawing -------------------------------------------------------------


async def test_a_chart_is_a_png():
    png = await charts.render_price_chart(
        points(88900, 87900, 79900), name="Un SSD", currency="MXN", target_cents=80000
    )

    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000  # an empty or failed render is a few dozen bytes


async def test_drawing_leaves_no_figure_behind():
    """The reason this module never imports `pyplot`.

    `pyplot` keeps a global registry and a notion of the current figure, so two charts
    rendered concurrently — the normal case for a bot, where every handler is a
    coroutine on one loop — would draw into each other. The failure is not a crash: it
    is the wrong picture sent to the right person. `Figure` owns nothing global, and
    this asserts that nothing quietly re-introduced the registry.
    """
    import matplotlib.pyplot as pyplot

    before = pyplot.get_fignums()
    await charts.render_price_chart(
        points(88900, 79900), name="Un SSD", currency="MXN", target_cents=80000
    )

    assert pyplot.get_fignums() == before == []


async def test_two_charts_render_at_once_without_interfering():
    import asyncio

    first, second = await asyncio.gather(
        charts.render_price_chart(
            points(88900, 79900), name="Primero", currency="MXN", target_cents=80000
        ),
        charts.render_price_chart(
            points(10000, 20000, 30000), name="Segundo", currency="MXN", target_cents=15000
        ),
    )

    assert first.startswith(PNG_MAGIC)
    assert second.startswith(PNG_MAGIC)
    # Different data has to produce different pictures. Identical bytes would be the
    # signature of both calls having drawn into one shared figure.
    assert first != second


async def test_an_out_of_stock_reading_changes_the_picture():
    """Out of stock is marked, not hidden. A chart that dropped it would draw a
    confident line through a period when the thing could not be bought."""
    available = await charts.render_price_chart(
        points(88900, 79900), name="Un SSD", currency="MXN", target_cents=80000
    )
    agotado = await charts.render_price_chart(
        points(88900, 79900, in_stock=False), name="Un SSD", currency="MXN", target_cents=80000
    )

    assert available != agotado


def x_labels(figure) -> list[str]:
    """The x-axis tick labels, which only exist once the figure has been drawn."""
    figure.canvas.draw()
    axes = figure.axes[0]
    return [label.get_text() for label in axes.get_xticklabels() if label.get_text()]


def test_readings_hours_apart_are_labelled_by_hour():
    """Found by looking at the first live chart, not by a test.

    Every reading of a freshly added product falls on one day, and a fixed "%d %b"
    formatter rendered that as the same date printed nine times across the axis. Nothing
    raised; the chart was simply useless. The formatter now picks its unit from the span
    it is given.
    """
    start = utc_now().replace(hour=9, minute=0, second=0, microsecond=0)
    same_day = [
        charts.PricePoint(start + timedelta(hours=offset), price, True)
        for offset, price in enumerate((88900, 88900, 85900, 79900))
    ]

    labels = x_labels(
        charts._build_figure(same_day, name="Un SSD", currency="MXN", target_cents=80000)
    )

    assert len(set(labels)) > 1, f"every tick reads the same: {labels}"
    assert any(":" in label for label in labels), f"no time of day on the axis: {labels}"


def test_readings_months_apart_are_labelled_by_date():
    spread = [
        charts.PricePoint(utc_now() - timedelta(days=days), price, True)
        for days, price in ((80, 88900), (50, 84900), (20, 79900), (1, 79500))
    ]

    labels = x_labels(
        charts._build_figure(spread, name="Un SSD", currency="MXN", target_cents=80000)
    )

    # The other half of the same decision: a long history must not be labelled in hours.
    assert len(set(labels)) > 1
    assert not any(":" in label for label in labels), f"hours on a three-month axis: {labels}"


async def test_a_chart_with_no_points_is_refused_rather_than_drawn():
    with pytest.raises(ValueError, match="no points"):
        await charts.render_price_chart([], name="Un SSD", currency="MXN", target_cents=80000)


def test_a_long_product_name_is_shortened_for_the_title():
    name = "SSD " + "muy largo " * 20

    assert len(charts._shorten(name)) <= 70
    assert charts._shorten("SSD Kingston A400") == "SSD Kingston A400"


# ---- /chart ------------------------------------------------------------------


async def test_chart_needs_a_number(resources):
    assert await commands.price_chart(resources, A_USER, []) == copy.CHART_USAGE
    assert await commands.price_chart(resources, A_USER, ["dos"]) == copy.CHART_USAGE
    assert await commands.price_chart(resources, A_USER, ["1", "2"]) == copy.CHART_USAGE


async def test_chart_of_a_position_that_is_not_there(resources):
    reply = await commands.price_chart(resources, A_USER, ["1"])

    assert reply == copy.CHART_NOT_FOUND


async def test_one_reading_is_not_enough_to_draw(resources):
    """What a user gets right after `/add`, which is when they are most likely to try.

    One point is a dot in an empty box. Saying so is better than sending a picture that
    looks like the feature is broken.
    """
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])

    reply = await commands.price_chart(resources, A_USER, ["1"])

    assert reply == copy.CHART_NOT_ENOUGH


async def test_a_tracked_product_with_history_charts(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        # `/add` wrote its reading at "now", so it is backdated to the start of the
        # story rather than the end of it. Otherwise the newest reading is the 889 the
        # fixture carries and the history reads as a rise.
        entry = await session.scalar(select(PriceHistory))
        entry.checked_at = utc_now() - timedelta(days=4)
        for offset, price in enumerate((87900, 84900, 79900), start=1):
            session.add(
                PriceHistory(
                    product_id=product.id,
                    price_cents=price,
                    in_stock=True,
                    checked_at=utc_now() - timedelta(days=4 - offset),
                )
            )

    result = await commands.price_chart(resources, A_USER, ["1"])

    assert isinstance(result, commands.Chart)
    assert result.png.startswith(PNG_MAGIC)
    # The caption carries the numbers that are hard to read off a plot precisely.
    assert "Kingston" in result.caption
    assert "Ahora: $799.00 MXN" in result.caption  # the newest reading
    assert "Mínimo: $799.00 MXN" in result.caption
    assert "máximo: $889.00 MXN" in result.caption  # what `/add` first saw
    assert "objetivo: $800.00 MXN" in result.caption
    assert "4 lecturas" in result.caption
    assert len(result.caption) <= 1024  # Telegram refuses a longer caption outright


async def test_readings_older_than_the_window_are_left_out(resources):
    await commands.add_tracking(resources, A_USER, [KINGSTON_URL, "800"])
    async with session_scope(resources.sessions) as session:
        product = await session.scalar(select(Product))
        session.add(
            PriceHistory(
                product_id=product.id,
                price_cents=50000,
                in_stock=True,
                checked_at=utc_now() - commands.CHART_WINDOW - timedelta(days=1),
            )
        )
        session.add(
            PriceHistory(
                product_id=product.id,
                price_cents=79900,
                in_stock=True,
                checked_at=utc_now() - timedelta(hours=1),
            )
        )

    result = await commands.price_chart(resources, A_USER, ["1"])

    assert isinstance(result, commands.Chart)
    # The ancient $500 reading would otherwise be reported as the all-time low of a
    # chart that does not show it — a caption disagreeing with its own picture.
    assert "$500.00 MXN" not in result.caption
    assert "2 lecturas" in result.caption
