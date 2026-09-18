"""Cyberpuerta: the first store, chosen by measurement (`docs/store-viability.md`).

Two things about this store shaped the code below, both established by reading real
pages rather than by assuming (the saved pages are in `tests/fixtures/`):

1. **The price is in clean JSON-LD.** `sku`, `mpn`, `gtin13`, `offers.price`,
   `offers.availability` and `offers.url` are all there. That is the easy half.

2. **The product's identity is not in the URL.** The same product answers on at least
   three different URLs, and the store itself disagrees with two of them:

       requested   /Computo-Hardware/.../SSD/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html
       redirect    /SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html          (301)
       canonical   /Computo-Hardware/.../SSD/SSD-Kingston-A400-240GB-2-5-SATA-III-
                   350-MB-s-Escritura-500-MB-s-Lectura.html

   This is the `(store, external_id)` rule in `docs/scraper-design.md` demonstrated in
   a single page load. A URL-keyed table would have held three rows for one SSD.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse, urlunparse

from price_tracker.money import to_cents
from price_tracker.scrapers.base import PageContent, Parser, ProductData
from price_tracker.scrapers.errors import (
    LayoutChangedError,
    ProductUnavailableError,
    UnsupportedUrlError,
)
from price_tracker.scrapers.jsonld import extract_jsonld_product

# Cyberpuerta runs OXID eShop, which gives every article a 32-hex internal id and
# exposes it as the non-SEO address of the page. This is what `external_id` is:
#
#   index.php?cl=details&anid=c156664ff9062de87fc3bf694dbb8eae
#
# The JSON-LD `sku` was the obvious alternative and was rejected. It holds the
# *manufacturer's* part number (`SA400S37/240G` — identical to `mpn`), so it says who
# made the thing, not which listing this is. Two listings of the same part, a bundle or
# a refurbished unit, would collide on it. The `anid` is the primary key of the store's
# own article table: unique by construction, and unchanged when the slug is rewritten.
#
# The cost is honest: `anid` sits in the page's serialised app state, not in a declared
# contract like JSON-LD, so it is the more likely of the two to move. That is a reason
# to notice when it disappears, which `LayoutChangedError` does, and not a reason to
# key the database on the wrong thing.
_ANID_IN_PAGE = re.compile(r"index\.php\?cl=details&(?:amp;)?anid=([0-9a-f]{32})")
_ANID = re.compile(r"^[0-9a-f]{32}$")

_HOSTS = frozenset({"cyberpuerta.mx", "www.cyberpuerta.mx"})
_CANONICAL_HOST = "www.cyberpuerta.mx"

# schema.org vocabulary that still means "you can buy this right now".
_AVAILABLE = frozenset({"InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly"})


class CyberpuertaParser(Parser):
    store = "cyberpuerta"

    def supports(self, url: str) -> bool:
        try:
            self.normalise_url(url)
        except UnsupportedUrlError:
            return False
        return True

    def normalise_url(self, url: str) -> str:
        """Reduce a pasted link to the page worth fetching.

        Query strings on this store are tracking (`?campaign=`, `?utm_source=`) and are
        dropped wholesale. The one exception is OXID's own `index.php?cl=details&anid=`
        form, where the query *is* the address — drop it and the link stops resolving.

        Note what this does not return: an `external_id`. A Cyberpuerta SEO URL simply
        does not contain one, so identity has to wait for the page itself. Inventing an
        id from the slug would have been the mistake, since the slug is exactly the part
        that changes.
        """
        parts = urlparse(url.strip())
        if parts.scheme not in ("http", "https", ""):
            raise UnsupportedUrlError(f"not an http(s) URL: {url!r}")

        if not parts.netloc:
            raise UnsupportedUrlError(f"not an absolute URL: {url!r}")
        if parts.netloc.lower() not in _HOSTS:
            raise UnsupportedUrlError(f"not a Cyberpuerta URL: {url!r}")

        path = parts.path or "/"

        if path.rsplit("/", 1)[-1] == "index.php":
            query = dict(parse_qsl(parts.query))
            anid = (query.get("anid") or "").lower()
            if query.get("cl") != "details" or not _ANID.match(anid):
                raise UnsupportedUrlError(f"not a Cyberpuerta product page: {url!r}")
            return urlunparse(("https", _CANONICAL_HOST, path, "", f"cl=details&anid={anid}", ""))

        if not path.endswith(".html"):
            # Category and search pages end in `/`. They are valid pages, just not
            # something with one price on it.
            raise UnsupportedUrlError(f"not a Cyberpuerta product page: {url!r}")

        return urlunparse(("https", _CANONICAL_HOST, path, "", "", ""))

    def parse(self, page: PageContent) -> ProductData:
        product = extract_jsonld_product(page.html)
        if product is None:
            raise LayoutChangedError(f"no schema.org Product in JSON-LD at {page.url}")

        if not product.name:
            raise LayoutChangedError(f"JSON-LD Product without a name at {page.url}")

        if product.price is None:
            # No offer block at all. Out of stock pages here keep their price and flip
            # `availability`, so reaching this means there is genuinely nothing to
            # record rather than something to record as unavailable.
            raise ProductUnavailableError(f"no offer on the page at {page.url}")

        try:
            price_cents = to_cents(product.price)
        except ValueError as exc:
            raise LayoutChangedError(f"unreadable price {product.price!r} at {page.url}") from exc

        if not product.currency:
            # Assuming MXN would be right today and silently wrong the day this store
            # starts quoting in USD, which is the kind of bug that shows up as money.
            raise LayoutChangedError(f"offer without a currency at {page.url}")

        match = _ANID_IN_PAGE.search(page.html)
        if match is None:
            raise LayoutChangedError(f"no OXID article id (anid) in the page at {page.url}")

        return ProductData(
            store=self.store,
            external_id=match.group(1),
            # The store's own declared address, not the one we happened to request.
            canonical_url=product.url or page.url,
            name=product.name.strip(),
            price_cents=price_cents,
            currency=product.currency.strip().upper(),
            # A priced offer with no stated availability is taken as buyable: the offer
            # existing is the stronger signal than the field being absent.
            in_stock=product.availability is None or product.availability in _AVAILABLE,
        )
