"""Drawing a product's price history as a PNG.

Two rules here are not style, and both of them bite in production rather than in a test.

**Never `pyplot`.** `pyplot` keeps a global registry of figures and a notion of the
"current" one. Two `/chart` commands running concurrently — which is the normal case for
a bot, since every handler is a coroutine on one loop — would draw into each other's
figure, and the result is not a crash but a wrong picture sent to the right person.
`Figure` plus `FigureCanvasAgg` owns nothing global, so a figure belongs to the call
that made it. It also skips the GUI backend entirely, which matters on a headless host.

**Rendering happens in a thread.** Matplotlib is synchronous and a plot takes long
enough to be felt; doing it on the event loop stalls every other handler and the
Telegram poll along with it. `asyncio.to_thread` is enough because the figure is local
to the call, which is the same property that makes the first rule work.

The module takes plain `PricePoint`s rather than `PriceHistory` rows. Partly so it can
be tested without a database, and partly because the rows would be crossing into another
thread after their session has closed — which happens to be safe here, and is not a
habit worth forming.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import matplotlib.dates as mdates
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

# Enough to read on a phone, small enough that Telegram does not recompress it into mud.
FIGURE_SIZE = (8.0, 4.5)
FIGURE_DPI = 130

LINE_COLOUR = "#1f77b4"
TARGET_COLOUR = "#d62728"
OUT_OF_STOCK_COLOUR = "#9467bd"
GRID_COLOUR = "#dddddd"


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One observation, as the chart needs it."""

    checked_at: datetime
    price_cents: int
    in_stock: bool


async def render_price_chart(
    points: Sequence[PricePoint],
    *,
    name: str,
    currency: str,
    target_cents: int,
) -> bytes:
    """Draw the history and return a PNG. Runs the drawing off the event loop."""
    return await asyncio.to_thread(
        _draw, points, name=name, currency=currency, target_cents=target_cents
    )


def _draw(
    points: Sequence[PricePoint],
    *,
    name: str,
    currency: str,
    target_cents: int,
) -> bytes:
    """The synchronous half. Pure: no globals touched, nothing left behind."""
    figure = _build_figure(points, name=name, currency=currency, target_cents=target_cents)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()


def _build_figure(
    points: Sequence[PricePoint],
    *,
    name: str,
    currency: str,
    target_cents: int,
) -> Figure:
    """Everything except writing the bytes out.

    Separate from `_draw` so a test can look at the result as a figure rather than as a
    PNG. Most of what this does is genuinely only judgeable by eye, but the date axis is
    not: whether it labels hours or months is a fact, and it is one that was wrong the
    first time a real chart was rendered.
    """
    if not points:
        raise ValueError("cannot draw a chart with no points")

    times = [point.checked_at for point in points]
    prices = [point.price_cents / 100 for point in points]
    target = target_cents / 100

    figure = Figure(figsize=FIGURE_SIZE, dpi=FIGURE_DPI, layout="constrained")
    # Attaching the canvas is what makes the figure renderable at all. A bare `Figure`
    # has `canvas = None`, and `savefig` on one fails on that rather than on anything
    # mentioning backends — normally `pyplot` is what quietly supplies this.
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    # A step, not a line. A straight segment between two readings claims the price moved
    # gradually between them, which is a thing we never saw: a price holds until the
    # next observation and then jumps. `steps-post` draws exactly what was measured.
    axes.plot(times, prices, drawstyle="steps-post", color=LINE_COLOUR, linewidth=2, zorder=3)
    axes.plot(times, prices, "o", color=LINE_COLOUR, markersize=3.5, zorder=4)

    axes.axhline(
        target,
        color=TARGET_COLOUR,
        linestyle="--",
        linewidth=1.4,
        zorder=2,
        label=f"Objetivo: {_money(target_cents, currency)}",
    )

    # Out of stock is data, not a gap — phase 1 settled that, and a chart that hid it
    # would draw a confident line through a week when the thing could not be bought.
    unavailable = [
        (point.checked_at, price)
        for point, price in zip(points, prices, strict=True)
        if not point.in_stock
    ]
    if unavailable:
        axes.plot(
            [time for time, _ in unavailable],
            [price for _, price in unavailable],
            "o",
            color=OUT_OF_STOCK_COLOUR,
            markersize=8,
            markerfacecolor="none",
            markeredgewidth=1.6,
            zorder=5,
            label="Agotado",
        )

    axes.set_title(_shorten(name), fontsize=11)
    axes.set_ylabel(f"Precio ({currency})", fontsize=9)
    axes.grid(True, color=GRID_COLOUR, linewidth=0.8, zorder=0)
    axes.set_axisbelow(True)
    axes.legend(fontsize=8, loc="best", framealpha=0.9)

    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)

    axes.yaxis.set_major_formatter(lambda value, _position: f"${value:,.0f}")

    # The date axis has to cope with both spans this bot produces: a product tracked for
    # months, and one added this morning whose readings are hours apart. A fixed
    # "%d %b" handles the first and renders the second as the same date repeated eight
    # times — which is what the first live chart actually looked like. `ConciseDateFormatter`
    # picks its unit from the range it is given, so hours show as hours.
    locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    # Headroom, so the target line is visible even when every reading sits above it —
    # which is the ordinary case for a product nobody has been alerted about yet.
    low = min(min(prices), target)
    high = max(max(prices), target)
    margin = max((high - low) * 0.12, 1.0)
    axes.set_ylim(low - margin, high + margin)

    return figure


def _money(cents: int, currency: str) -> str:
    """Prices on the chart, short. `money.format_cents` is for chat, not for a legend."""
    return f"${cents / 100:,.0f} {currency}"


def _shorten(name: str, limit: int = 70) -> str:
    """Store product names run long enough to render as a title in two-point type."""
    return name if len(name) <= limit else f"{name[: limit - 1].rstrip()}…"
