"""Read a schema.org Product out of a page's JSON-LD.

JSON-LD is tried before CSS selectors because it is a semi-formal contract, and stores
break their class names far more often than their structured data. Cyberpuerta ships a
clean one; Liverpool ships none, which is the whole reason it was picked as store #2.

This graduated out of `scripts/store_spike.py`, along with its tests. The spike keeps
its own copy on purpose: it is a throwaway measurement tool whose findings are already
recorded in `docs/store-viability.md`, and it must not become a dependency of the
application. The copy here is free to grow fields the spike never needed — `sku`,
`availability`, the offer URL — without touching evidence that has already been filed.

Three shapes all appear in the wild and all three are handled: a bare object, a list,
and an `@graph` wrapper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from selectolax.lexbor import LexborHTMLParser

# schema.org values arrive either bare ("InStock") or as a full URL
# ("https://schema.org/InStock"). Both mean the same thing.
_SCHEMA_PREFIXES = ("https://schema.org/", "http://schema.org/", "https://schema.org#")


@dataclass(frozen=True, slots=True)
class JsonLdProduct:
    """What a `schema.org/Product` block said, as text, before interpretation.

    Everything is optional and nothing is converted here: prices stay strings in the
    store's own formatting, and `availability` keeps schema.org's vocabulary. Deciding
    what a missing price or an unknown availability *means* is the store parser's job,
    because the answer differs per store.
    """

    name: str | None = None
    price: str | None = None
    currency: str | None = None
    sku: str | None = None
    availability: str | None = None
    url: str | None = None


def _strip_schema_prefix(value: str) -> str:
    for prefix in _SCHEMA_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _as_text(value: Any) -> str | None:
    """Keep scalars as written; ignore nested objects a field should not have."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return None
    return str(value)


def _candidates(data: Any) -> list[dict]:
    """Flatten one JSON-LD block into the objects it might contain."""
    if isinstance(data, dict):
        graph = data.get("@graph")
        return [d for d in graph if isinstance(d, dict)] if isinstance(graph, list) else [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _is_product(entry: dict) -> bool:
    entry_type = entry.get("@type")
    types = entry_type if isinstance(entry_type, list) else [entry_type]
    return any(isinstance(t, str) and _strip_schema_prefix(t) == "Product" for t in types)


def _read_offer(offers: Any) -> tuple[str | None, str | None, str | None, str | None]:
    """Pull (price, currency, availability, url) out of an `offers` value.

    A page may carry one offer, several, or none. Where there are several this takes
    the first: the alternative is picking the cheapest, which would quietly track a
    different seller's price than the one the user saw.
    """
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict)), None)
    if not isinstance(offers, dict):
        return None, None, None, None

    price = _as_text(offers.get("price"))
    if price is None:
        # An AggregateOffer has no `price`; it has a range.
        price = _as_text(offers.get("lowPrice"))

    availability = _as_text(offers.get("availability"))
    if availability is not None:
        availability = _strip_schema_prefix(availability)

    return price, _as_text(offers.get("priceCurrency")), availability, _as_text(offers.get("url"))


def extract_jsonld_product(html: str) -> JsonLdProduct | None:
    """Find the first `schema.org/Product` in the page, or `None` if there is none.

    A block of malformed JSON is skipped rather than raised on: a store shipping one
    broken script tag must degrade to "price not found", never take a run down.
    """
    tree = LexborHTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for entry in _candidates(data):
            if not _is_product(entry):
                continue
            price, currency, availability, url = _read_offer(entry.get("offers"))
            return JsonLdProduct(
                name=_as_text(entry.get("name")),
                price=price,
                currency=currency,
                sku=_as_text(entry.get("sku")),
                availability=availability,
                url=url,
            )
    return None
