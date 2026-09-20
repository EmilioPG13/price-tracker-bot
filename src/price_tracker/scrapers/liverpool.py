"""Liverpool: the second store, chosen because it is the harder one.

Cyberpuerta ships a clean `schema.org/Product`; Liverpool ships none at all. That was
the reason for picking it (`docs/store-viability.md`) — a second JSON-LD store would
have proved nothing about `Fetcher` and `Parser` being separate types. Everything below
reads the page differently from `cyberpuerta.py`, and nothing outside this module needed
to change to accommodate it.

Four things about this store shaped the code, all established by reading two captured
pages rather than by assuming (they are in `tests/fixtures/`):

1. **The price is in an embedded payload, not in the markup.** The page is a Next.js App
   Router app and streams its state through `self.__next_f.push(...)`. `nextjs.py`
   reassembles it. This was the open question phase 5 had to answer first, and the
   answer is the good one: a parser against a data structure, not against class names.

2. **The page carries 111 other products.** Recommendation carousels ship a full record
   each, priced. "First price on the page" would have tracked whatever Liverpool felt
   like recommending that morning. The main product is the only one under a
   `productInfo` key — the carousels sit under `priceInfo` with a different shape
   (`minimumPromoPrice`/`maximumListPrice`), which is what makes the key a safe anchor.

3. **`salePrice` is not the price you pay.** On a discounted product the payload reads
   `salePrice: 7999, listPrice: {price: 7999}, promoPrice: {price: 6399.2}` and the page
   renders `$6,399.20`. `promoPrice.price` is the one the customer is charged;
   `salePrice` is the price before the discount. The undiscounted fixture has all three
   equal, so a parser written against that page alone would have looked correct and
   quietly recorded the wrong number on every product on sale.

4. **The store never states a currency for its own products.** `currencyIsoCode` appears
   only on third-party marketplace offers; a first-party product page has none. See
   `CURRENCY` below for what is done about that, which is the one assumption in this
   file.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from selectolax.lexbor import LexborHTMLParser

from price_tracker.money import to_cents
from price_tracker.scrapers.base import PageContent, Parser, ProductData
from price_tracker.scrapers.errors import (
    LayoutChangedError,
    ProductUnavailableError,
    UnsupportedUrlError,
)
from price_tracker.scrapers.nextjs import json_object, rsc_payload

_HOSTS = frozenset({"liverpool.com.mx", "www.liverpool.com.mx"})
_CANONICAL_HOST = "www.liverpool.com.mx"

# A product page is `/tienda/pdp/<slug>/<id>`. The id is digits of no fixed width: the
# captures and the links inside them carry 10, 11 and 12 digit ids (`1141535451`,
# `99991606363`, `999673847131`), so pinning a length would reject real products.
_PDP_PATH = re.compile(r"^/tienda/pdp/(?P<slug>[^/]+)/(?P<id>\d+)/?$")

# Liverpool quotes in Mexican pesos and the payload does not say so for its own
# products. Cyberpuerta's parser refuses to assume a currency, and that is the right
# rule where the store declares one — assuming would be right today and silently wrong
# the day a store quotes in USD. Here there is nothing to read, so the choice is between
# an assumption and not supporting the store at all.
#
# It is made an assumption *with a tripwire* rather than a bare constant: where the page
# does declare currencies, on marketplace offers, MXN must be among them. That will not
# fire on a mixed page — a cross-border listing may legitimately quote another currency
# alongside pesos — but it does fire if the store moves off pesos wholesale, which is
# the scenario that would otherwise corrupt every stored price without raising anything.
CURRENCY = "MXN"
_DECLARED_CURRENCY = re.compile(r'"currencyIsoCode"\s*:\s*"([A-Za-z]{3})"')


def _text(value: object) -> str:
    """A payload string, or `""`.

    Next.js elides a value it did not send as the *string* `"$undefined"`, and a Flight
    reference to another row as `"$5e7"`. Both are placeholders rather than data, and
    both would otherwise end up in a product name shown to a user.
    """
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    return "" if stripped.startswith("$") else stripped


class LiverpoolParser(Parser):
    store = "liverpool"

    def supports(self, url: str) -> bool:
        try:
            self.normalise_url(url)
        except UnsupportedUrlError:
            return False
        return True

    def normalise_url(self, url: str) -> str:
        """Reduce a pasted link to the page worth fetching.

        The whole query string is dropped. Most of it is tracking, but one parameter is
        worth naming: a share link carries `?skuId=`, which selects a variant — a size
        or a colour — of the same listing. Dropping it is deliberate rather than
        careless. `external_id` is the *product* id, which every variant shares, so two
        variant links would collapse to one row in `products` anyway; keeping the
        parameter would only mean the stored `canonical_url` flapped between whichever
        variant was pasted last. The cost is real and worth stating: this bot tracks a
        listing's price, not a particular size's.
        """
        parts = urlparse(url.strip())
        if parts.scheme not in ("http", "https", ""):
            raise UnsupportedUrlError(f"not an http(s) URL: {url!r}")

        if not parts.netloc:
            raise UnsupportedUrlError(f"not an absolute URL: {url!r}")
        if parts.netloc.lower() not in _HOSTS:
            raise UnsupportedUrlError(f"not a Liverpool URL: {url!r}")

        match = _PDP_PATH.match(parts.path or "/")
        if match is None:
            # Category, search and the front page all live under other paths. They are
            # valid pages, just not ones with a single price on them.
            raise UnsupportedUrlError(f"not a Liverpool product page: {url!r}")

        path = f"/tienda/pdp/{match['slug']}/{match['id']}"
        return urlunparse(("https", _CANONICAL_HOST, path, "", "", ""))

    def parse(self, page: PageContent) -> ProductData:
        payload = rsc_payload(page.html)
        if not payload:
            raise LayoutChangedError(f"no Next.js payload in the page at {page.url}")

        # The anchor. Every other priced record in this payload belongs to a carousel.
        info = json_object(payload, "productInfo")
        if info is None:
            raise LayoutChangedError(f"no productInfo in the payload at {page.url}")

        external_id = _text(info.get("productId"))
        if not external_id:
            raise LayoutChangedError(f"productInfo without a productId at {page.url}")

        name = self._name(info)
        if not name:
            raise LayoutChangedError(f"productInfo without a title at {page.url}")

        price_info = info.get("priceInfo")
        if not isinstance(price_info, dict):
            # No prices at all on a page that is otherwise a product page. Nothing to
            # record, as opposed to something to record as unavailable.
            raise ProductUnavailableError(f"no priceInfo on the page at {page.url}")

        price_cents = self._price(price_info, page.url)

        declared = {code.upper() for code in _DECLARED_CURRENCY.findall(payload)}
        if declared and CURRENCY not in declared:
            raise LayoutChangedError(
                f"page declares {sorted(declared)} and never {CURRENCY} at {page.url}"
            )

        # A bool or nothing. A string here would be read as `True` by any truthiness
        # test, including the string "false", which is the same class of silent wrong
        # answer that keeps money out of floats in this project.
        in_stock = info.get("inventoryStatus", True)
        if not isinstance(in_stock, bool):
            raise LayoutChangedError(
                f"inventoryStatus is {type(in_stock).__name__}, not a bool, at {page.url}"
            )

        return ProductData(
            store=self.store,
            external_id=external_id,
            canonical_url=self._canonical_url(page),
            name=name,
            price_cents=price_cents,
            currency=CURRENCY,
            in_stock=in_stock,
        )

    def _name(self, info: dict) -> str:
        """`title`, with the brand in front of it when the title omits it.

        The payload splits them — `brand: "OSTER"`, `title: "Licuadora 2110245 2
        velocidades"` — and the title alone is what the page's `h1` shows. The brand is
        added back because the name's job here is to be recognisable in a `/list` of
        products from several stores, where "Licuadora 2110245" on its own is not.
        Cyberpuerta's names lead with the manufacturer for the same reason.
        """
        title = _text(info.get("title"))
        brand = _text(info.get("brand"))
        if not title or not brand or brand.lower() in title.lower():
            return title
        return f"{brand} {title}"

    def _price(self, price_info: dict, url: str) -> int:
        """`promoPrice.price` — what the customer is charged — in cents.

        Deliberately not `salePrice`, and deliberately without a fallback to it. On a
        discounted product `salePrice` holds the *pre-discount* price, so falling back
        would not degrade gracefully: it would record a number that is wrong by exactly
        the size of the discount, on the products a price tracker exists to catch.
        Failing loudly is the only honest option, and `LayoutChangedError` is already
        the type that means "this repo is out of date".
        """
        promo = price_info.get("promoPrice")
        if not isinstance(promo, dict) or "price" not in promo:
            raise LayoutChangedError(f"priceInfo without a promoPrice.price at {url}")

        try:
            return to_cents(promo["price"])
        except ValueError as exc:
            raise LayoutChangedError(f"unreadable price {promo['price']!r} at {url}") from exc

    def _canonical_url(self, page: PageContent) -> str:
        """The address the store declares for this product, not the one we requested.

        Liverpool publishes it as `<link rel="canonical">`. It matters for the same
        reason it did at Cyberpuerta: the payload's own share link uses a *different*
        slug for the same product (`licuadora-2110245…` where the canonical says
        `licuadora-oster-2110245…`), so "the URL" is not one thing and the store's
        declared answer is the one to store and show.
        """
        node = LexborHTMLParser(page.html).css_first('link[rel="canonical"]')
        href = (node.attributes.get("href") or "").strip() if node else ""
        # Resolved against the page in case the store ever makes it relative; a relative
        # canonical stored as-is would be shown to a user as an unclickable fragment.
        return urljoin(page.url, href) if href else page.url
