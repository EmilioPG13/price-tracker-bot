"""Tests for the seam the rest of the application uses.

`fetch_product` is the single entry point shared by the `/add` command handler and the
scheduled price checker. The two runtimes are the reason the service layer exists at
all, so the thing worth testing here is that routing a URL to a store and reading it
are one code path, not two.
"""

import pytest

from price_tracker.scrapers import (
    PARSERS,
    CyberpuertaParser,
    Fetcher,
    LiverpoolParser,
    PageContent,
    UnsupportedUrlError,
    fetch_product,
    parser_for,
)

PRODUCT = (
    "https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/"
    "SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"
)


class RecordingFetcher(Fetcher):
    """A fetcher that serves a saved page and remembers what it was asked for."""

    def __init__(self, html: str) -> None:
        self.html = html
        self.requested: list[str] = []

    async def fetch(self, url: str) -> PageContent:
        self.requested.append(url)
        return PageContent(url=url, html=self.html)


LIVERPOOL_PRODUCT = (
    "https://www.liverpool.com.mx/tienda/pdp/licuadora-oster-2110245-2-velocidades/1141535451"
)


def test_a_cyberpuerta_url_routes_to_its_parser():
    assert isinstance(parser_for(PRODUCT), CyberpuertaParser)


def test_a_liverpool_url_routes_to_its_parser():
    # Two stores now, and the routing is the only place that knows there is more than
    # one. Nothing downstream of `fetch_product` learns a store's name.
    assert isinstance(parser_for(LIVERPOOL_PRODUCT), LiverpoolParser)


def test_each_store_declines_the_others_urls():
    # `parser_for` returns the first parser that claims a URL, so a parser whose
    # `supports` was too generous would silently swallow the other store's links.
    assert [p.store for p in PARSERS if p.supports(PRODUCT)] == ["cyberpuerta"]
    assert [p.store for p in PARSERS if p.supports(LIVERPOOL_PRODUCT)] == ["liverpool"]


def test_an_unknown_store_is_rejected_before_any_request():
    # Mercado Libre is deferred, not supported (`docs/store-viability.md`). This is the
    # honest answer, and no request is made to a store we cannot read.
    with pytest.raises(UnsupportedUrlError):
        parser_for("https://articulo.mercadolibre.com.mx/MLM-656960312-licuadora-oster-_JM")


async def test_fetches_the_normalised_url_not_the_pasted_one(load_fixture):
    fetcher = RecordingFetcher(load_fixture("cyberpuerta-ssd-kingston-a400.html"))

    product = await fetch_product(PRODUCT + "?utm_source=telegram", fetcher)

    assert fetcher.requested == [PRODUCT]
    assert product.external_id == "c156664ff9062de87fc3bf694dbb8eae"
    assert product.price_cents == 88900
