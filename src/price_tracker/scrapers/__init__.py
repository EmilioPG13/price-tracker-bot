"""Reading a product from a store page.

The surface the rest of the app uses is `fetch_product(url, fetcher)`; everything else
here is for tests and for the next store. Adding a store means writing a `Parser` and
adding it to `PARSERS` — nothing else in the application should learn its name.
"""

from __future__ import annotations

from price_tracker.scrapers.base import Fetcher, PageContent, Parser, ProductData
from price_tracker.scrapers.cyberpuerta import CyberpuertaParser
from price_tracker.scrapers.errors import (
    FetchError,
    FetchTimeoutError,
    LayoutChangedError,
    PageGoneError,
    ProductUnavailableError,
    ScraperError,
    StoreRefusedError,
    UnsupportedUrlError,
)
from price_tracker.scrapers.http import HttpFetcher, RobotsDisallowedError
from price_tracker.scrapers.jsonld import JsonLdProduct, extract_jsonld_product

# Ordered, and asked in order. Liverpool joins in phase 5.
PARSERS: tuple[Parser, ...] = (CyberpuertaParser(),)


def parser_for(url: str) -> Parser:
    """Find the parser that claims this URL.

    Raises:
        UnsupportedUrlError: no store we support recognises it. This is the common case
            for a link a user pasted, so the message it carries is user-facing material.
    """
    for parser in PARSERS:
        if parser.supports(url):
            return parser
    raise UnsupportedUrlError(f"no supported store for {url!r}")


async def fetch_product(url: str, fetcher: Fetcher) -> ProductData:
    """Read one product, start to finish.

    Both callers of the service layer end up here: the `/add` command, which needs an
    answer now, and the scheduled checker, which does not care how long it takes. They
    share this function precisely so the second one cannot drift from the first.

    Raises:
        ScraperError: one of its subclasses, each of which maps to a different reply.
    """
    parser = parser_for(url)
    page = await fetcher.fetch(parser.normalise_url(url))
    return parser.parse(page)


__all__ = [
    "PARSERS",
    "CyberpuertaParser",
    "FetchError",
    "FetchTimeoutError",
    "Fetcher",
    "HttpFetcher",
    "JsonLdProduct",
    "LayoutChangedError",
    "PageContent",
    "PageGoneError",
    "Parser",
    "ProductData",
    "ProductUnavailableError",
    "RobotsDisallowedError",
    "ScraperError",
    "StoreRefusedError",
    "UnsupportedUrlError",
    "extract_jsonld_product",
    "fetch_product",
    "parser_for",
]
