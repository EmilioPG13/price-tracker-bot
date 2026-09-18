"""Tests for the Cyberpuerta parser, against real captured pages.

The two fixtures are pages the store actually served on 2026-09-18 (provenance is in
`tests/fixtures/README.md`). Testing against a capture rather than against hand-written
markup is the point: hand-written markup tests the parser against our own assumptions,
which is how a parser passes its suite and fails on the store.
"""

import pytest

from price_tracker.scrapers import (
    CyberpuertaParser,
    LayoutChangedError,
    PageContent,
    ProductUnavailableError,
)

KINGSTON = "cyberpuerta-ssd-kingston-a400.html"
ACER = "cyberpuerta-ssd-acer-gm7.html"

# The URL that was requested to capture the Kingston page. Note that the store answered
# it with a 301 to a flat path, and then declared a *third* URL as canonical.
REQUESTED = "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"


@pytest.fixture
def parser():
    return CyberpuertaParser()


def test_reads_a_real_product_page(parser, load_fixture):
    product = parser.parse(PageContent(url=REQUESTED, html=load_fixture(KINGSTON)))

    assert product.store == "cyberpuerta"
    assert product.name == 'Kingston SA400S37/240G SSD 2.5" SATA III'
    assert product.price_cents == 88900  # the page said 889, unquoted
    assert product.currency == "MXN"
    assert product.in_stock is True


def test_external_id_is_the_store_article_id_not_the_manufacturer_sku(parser, load_fixture):
    product = parser.parse(PageContent(url=REQUESTED, html=load_fixture(KINGSTON)))

    # The JSON-LD also offers sku "SA400S37/240G", which is the manufacturer's part
    # number and identifies the hardware, not this listing.
    assert product.external_id == "c156664ff9062de87fc3bf694dbb8eae"


def test_canonical_url_comes_from_the_store_not_from_the_request(parser, load_fixture):
    product = parser.parse(PageContent(url=REQUESTED, html=load_fixture(KINGSTON)))

    # Three spellings of one product in a single page load. The store's own answer wins.
    assert product.canonical_url == (
        "https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/"
        "SSD-Kingston-A400-240GB-2-5-SATA-III-350-MB-s-Escritura-500-MB-s-Lectura.html"
    )
    assert product.canonical_url != REQUESTED


def test_a_second_real_page_parses_too(parser, load_fixture):
    # One fixture proves the parser reads one page; two prove it reads the template.
    product = parser.parse(
        PageContent(url="https://www.cyberpuerta.mx/x.html", html=load_fixture(ACER))
    )

    assert product.external_id == "494415ec3f92405919da43332b38a307"
    assert product.name == "Acer NVMe 1TB PCIe4.0 6300/7200MB/s BL.9BWWR.118"
    assert product.price_cents == 393900
    assert product.in_stock is True


def test_the_two_fixtures_have_different_ids(parser, load_fixture):
    # A regex that accidentally matched something shared by every page — an analytics
    # id, a session token — would still pass every test above.
    first = parser.parse(PageContent(url=REQUESTED, html=load_fixture(KINGSTON)))
    second = parser.parse(PageContent(url=REQUESTED, html=load_fixture(ACER)))
    assert first.external_id != second.external_id


# --- The failure modes, on synthetic pages -----------------------------------------
#
# These are written by hand on purpose: capturing a real out-of-stock page means
# waiting for Cyberpuerta to run out of something.

ANID = "index.php?cl=details&anid=c156664ff9062de87fc3bf694dbb8eae"


def product_page(jsonld: str, *, with_anid: bool = True) -> str:
    tail = f'<a href="{ANID}">ficha</a>' if with_anid else ""
    return (
        f'<html><head><script type="application/ld+json">{jsonld}</script></head>'
        f"<body>{tail}</body></html>"
    )


def test_out_of_stock_is_data_not_an_error(parser):
    # The interesting case to keep tracking: it may come back cheaper.
    html = product_page("""
        {"@type":"Product","name":"Agotado","sku":"X",
         "offers":{"price":"100","priceCurrency":"MXN",
                   "availability":"https://schema.org/OutOfStock"}}
    """)
    product = parser.parse(PageContent(url="https://www.cyberpuerta.mx/x.html", html=html))
    assert product.in_stock is False
    assert product.price_cents == 10000


def test_a_page_with_no_offer_at_all_is_unavailable(parser):
    html = product_page('{"@type":"Product","name":"Sin oferta"}')
    with pytest.raises(ProductUnavailableError):
        parser.parse(PageContent(url="https://www.cyberpuerta.mx/x.html", html=html))


@pytest.mark.parametrize(
    ("jsonld", "with_anid", "reason"),
    [
        ('{"@type":"Article","name":"Blog"}', True, "no Product at all"),
        ('{"@type":"Product","offers":{"price":"1","priceCurrency":"MXN"}}', True, "no name"),
        (
            '{"@type":"Product","name":"X","offers":{"price":"a la venta","priceCurrency":"MXN"}}',
            True,
            "price is not a number",
        ),
        ('{"@type":"Product","name":"X","offers":{"price":"1"}}', True, "no currency"),
        (
            '{"@type":"Product","name":"X","offers":{"price":"1","priceCurrency":"MXN"}}',
            False,
            "no article id in the page",
        ),
    ],
)
def test_a_page_we_cannot_read_says_so(parser, jsonld, with_anid, reason):
    # Each of these means our parser is out of date, which is a different thing from
    # the user's link being wrong or the product being sold out.
    html = product_page(jsonld, with_anid=with_anid)
    with pytest.raises(LayoutChangedError):
        parser.parse(PageContent(url=f"https://www.cyberpuerta.mx/{reason}.html", html=html))


def test_a_missing_currency_is_never_assumed_to_be_pesos(parser):
    # Defaulting would be right today and silently wrong the day this store quotes USD.
    html = product_page('{"@type":"Product","name":"X","offers":{"price":"1"}}')
    with pytest.raises(LayoutChangedError, match="currency"):
        parser.parse(PageContent(url="https://www.cyberpuerta.mx/x.html", html=html))
