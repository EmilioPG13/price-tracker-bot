"""Tests for the Liverpool parser, against real captured pages.

Both fixtures are pages the store served on 2026-09-19 (provenance in
`tests/fixtures/README.md`). Two of the tests below are here because writing the parser
against one page would have produced something that passed its suite and recorded the
wrong number in production:

- the page carries 111 *other* priced products, for its recommendation carousels, so a
  parser that finds "the price" rather than "this product's price" looks correct;
- on an undiscounted product `salePrice`, `listPrice` and `promoPrice` are all equal, so
  the difference between them is invisible until a discounted page is read.
"""

import json

import pytest

from price_tracker.scrapers import (
    LayoutChangedError,
    LiverpoolParser,
    PageContent,
    ProductUnavailableError,
)

LICUADORA = "liverpool-licuadora-oster-2110245.html"
BATIDORA = "liverpool-batidora-oster-1110425673.html"

LICUADORA_URL = (
    "https://www.liverpool.com.mx/tienda/pdp/licuadora-oster-2110245-2-velocidades/1141535451"
)
BATIDORA_URL = (
    "https://www.liverpool.com.mx/tienda/pdp/batidora-de-pedestal-10-velocidades/1110425673"
)


@pytest.fixture
def parser():
    return LiverpoolParser()


# --- The real pages ------------------------------------------------------------------


def test_reads_a_real_product_page(parser, load_fixture):
    product = parser.parse(PageContent(url=LICUADORA_URL, html=load_fixture(LICUADORA)))

    assert product.store == "liverpool"
    assert product.name == "OSTER Licuadora 2110245 2 velocidades"
    assert product.price_cents == 107800  # the page rendered $1,078.00
    assert product.currency == "MXN"
    assert product.in_stock is True


def test_external_id_is_the_store_listing_id(parser, load_fixture):
    product = parser.parse(PageContent(url=LICUADORA_URL, html=load_fixture(LICUADORA)))

    # Read from the payload rather than sliced off the URL, even though Liverpool does
    # put it in the URL and Cyberpuerta does not. The page is the authority on what it
    # is about; a link can arrive after a redirect, or with a `?skuId=` that names a
    # variant rather than the listing.
    assert product.external_id == "1141535451"


def test_the_price_is_the_products_own_and_not_a_carousel_neighbours(parser, load_fixture):
    html = load_fixture(LICUADORA)
    product = parser.parse(PageContent(url=LICUADORA_URL, html=html))

    # The trap this asserts against is real, not hypothetical: these are prices of
    # recommended products sitting in the same payload, and the first of them appears
    # *before* the product's own price in the document.
    assert '"promoPrice":2149' in html.replace("\\", "")
    assert product.price_cents == 107800


def test_a_discounted_page_records_what_the_customer_pays(parser, load_fixture):
    product = parser.parse(PageContent(url=BATIDORA_URL, html=load_fixture(BATIDORA)))

    # The payload says salePrice 7999, listPrice 7999, promoPrice 6399.2 and the page
    # renders "$6,399.20" struck through with "$7,999.00". Reading salePrice here would
    # be wrong by exactly the discount — on precisely the products this bot exists for.
    assert product.price_cents == 639920
    assert product.price_cents != 799900


def test_a_second_real_page_parses_too(parser, load_fixture):
    # One fixture proves the parser reads one page; two prove it reads the template.
    product = parser.parse(PageContent(url=BATIDORA_URL, html=load_fixture(BATIDORA)))

    assert product.external_id == "1110425673"
    assert product.name == "OSTER Batidora de pedestal 10 velocidades"
    assert product.in_stock is True


def test_canonical_url_comes_from_the_store_not_from_the_request(parser, load_fixture):
    requested = "https://www.liverpool.com.mx/tienda/pdp/x/1141535451"
    product = parser.parse(PageContent(url=requested, html=load_fixture(LICUADORA)))

    # The payload's own share link spells the slug `licuadora-2110245-…`; the canonical
    # link says `licuadora-oster-2110245-…`. One product, more than one address, again.
    assert product.canonical_url == LICUADORA_URL


def test_the_two_fixtures_have_different_ids(parser, load_fixture):
    # A reader that had latched onto something every page shares would pass the tests
    # above and fail here.
    first = parser.parse(PageContent(url=LICUADORA_URL, html=load_fixture(LICUADORA)))
    second = parser.parse(PageContent(url=BATIDORA_URL, html=load_fixture(BATIDORA)))
    assert first.external_id != second.external_id


# --- The failure modes, on synthetic pages -------------------------------------------
#
# Written by hand, because capturing a real sold-out page means waiting for Liverpool to
# run out of something, and the rest of these are pages the store has never served.

PRICE_INFO = {"salePrice": 100, "listPrice": {"price": 100}, "promoPrice": {"price": 100}}


def product_info(**overrides) -> dict:
    info = {
        "title": "Licuadora 2110245 2 velocidades",
        "brand": "OSTER",
        "productId": "1141535451",
        "inventoryStatus": True,
        "priceInfo": dict(PRICE_INFO),
    }
    info.update(overrides)
    return {key: value for key, value in info.items() if value is not ...}


def page(payload: str, *, canonical: str = "") -> str:
    """Wrap a Flight payload the way Next.js streams it: several appends to a queue.

    Deliberately split across two chunks. Reassembly is half of what `nextjs.py` does,
    and a single-chunk fixture would never exercise it.
    """
    link = f'<link rel="canonical" href="{canonical}"/>' if canonical else ""
    half = len(payload) // 2
    scripts = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
        for part in (payload[:half], payload[half:])
    )
    return f"<html><head>{link}</head><body>{scripts}</body></html>"


def product_page(**overrides) -> str:
    return page("3a:" + json.dumps({"productInfo": product_info(**overrides)}))


def parse(parser, html: str):
    return parser.parse(PageContent(url="https://www.liverpool.com.mx/tienda/pdp/x/1", html=html))


def test_the_payload_is_reassembled_from_its_chunks(parser):
    # The happy path of the synthetic builder, so that a failure in the tests below
    # means what it says rather than "the fake page was malformed".
    product = parse(parser, product_page())
    assert product.price_cents == 10000
    assert product.external_id == "1141535451"


def test_out_of_stock_is_data_not_an_error(parser):
    # The interesting case to keep tracking: it may come back cheaper.
    product = parse(parser, product_page(inventoryStatus=False))
    assert product.in_stock is False
    assert product.price_cents == 10000


def test_a_missing_inventory_status_is_taken_as_buyable(parser):
    # Same rule as the Cyberpuerta parser applies to a missing `availability`: a priced
    # product with nothing said about stock is more likely for sale than not.
    product = parse(parser, product_page(inventoryStatus=...))
    assert product.in_stock is True


def test_a_page_with_no_prices_at_all_is_unavailable(parser):
    with pytest.raises(ProductUnavailableError):
        parse(parser, product_page(priceInfo=...))


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("<html><body><p>hola</p></body></html>", "no payload at all"),
        (page('3a:{"somethingElse":{"a":1}}'), "payload without productInfo"),
        (page('3a:{"productInfo":{"title":"X"'), "payload truncated mid-object"),
    ],
)
def test_a_page_we_cannot_read_says_so(parser, html, reason):
    with pytest.raises(LayoutChangedError):
        parse(parser, html)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"productId": ...}, "no product id"),
        ({"productId": "$undefined"}, "product id elided by the framework"),
        ({"title": ..., "brand": ...}, "no title"),
        ({"priceInfo": {"salePrice": 100}}, "no promoPrice"),
        ({"priceInfo": {"promoPrice": {}}}, "promoPrice without a price"),
        ({"priceInfo": {"promoPrice": {"price": "a la venta"}}}, "price is not a number"),
        ({"inventoryStatus": "false"}, "inventoryStatus is a string"),
    ],
)
def test_a_productinfo_we_cannot_read_says_so(parser, overrides, reason):
    # Each of these means this repo is out of date, which is a different thing from the
    # user's link being wrong or the product being sold out.
    with pytest.raises(LayoutChangedError):
        parse(parser, product_page(**overrides))


def test_the_string_false_is_never_read_as_in_stock(parser):
    # Worth its own test rather than a row in the table above. `bool("false")` is True,
    # so the failure this prevents is not a crash: it is a sold-out product quietly
    # reported as buyable.
    with pytest.raises(LayoutChangedError, match="inventoryStatus"):
        parse(parser, product_page(inventoryStatus="false"))


# --- The one assumption in the parser ------------------------------------------------


def test_a_page_quoting_only_another_currency_is_refused(parser):
    # Liverpool never declares a currency for its own products, so the parser assumes
    # pesos. This is the tripwire on that assumption: if the only currencies the page
    # does declare are not MXN, the assumption has stopped being safe.
    html = page("3a:" + json.dumps({"productInfo": product_info()}) + ',{"currencyIsoCode":"USD"}')
    with pytest.raises(LayoutChangedError, match="MXN"):
        parse(parser, html)


def test_a_marketplace_page_quoting_pesos_among_others_is_fine(parser):
    # A cross-border listing may legitimately carry an offer in another currency beside
    # the peso ones. That must not take the page down.
    html = page(
        "3a:"
        + json.dumps({"productInfo": product_info()})
        + ',{"currencyIsoCode":"USD"},{"currencyIsoCode":"MXN"}'
    )
    assert parse(parser, html).currency == "MXN"


def test_the_currency_is_pesos_when_the_page_declares_none(parser):
    # The common case: a first-party product page states no currency anywhere.
    product = parse(parser, product_page())
    assert product.currency == "MXN"


# --- The name ------------------------------------------------------------------------


def test_the_brand_is_put_in_front_of_the_title(parser):
    # The payload splits them and the page's h1 shows the title alone, which in a /list
    # of products from several stores is not enough to tell them apart.
    assert parse(parser, product_page()).name == "OSTER Licuadora 2110245 2 velocidades"


def test_a_brand_already_in_the_title_is_not_repeated(parser):
    product = parse(parser, product_page(title="Licuadora Oster 2110245", brand="OSTER"))
    assert product.name == "Licuadora Oster 2110245"


def test_a_title_with_no_brand_stands_on_its_own(parser):
    product = parse(parser, product_page(brand="$undefined"))
    assert product.name == "Licuadora 2110245 2 velocidades"
