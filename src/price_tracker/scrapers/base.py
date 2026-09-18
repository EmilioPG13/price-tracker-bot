"""The two halves of a scraper, and the record they produce.

`Fetcher` and `Parser` are deliberately separate rather than one `Scraper` class,
because the two things that go wrong are unrelated and arrive on different days:

- the store changes its HTML      -> a new Parser, same Fetcher
- the store blocks our requests   -> a new Fetcher (an official API, say), same Parser

Keeping them apart also keeps the parser synchronous and free of I/O, which is why
every parser test in this repo runs against a saved file and never touches the network.

Liverpool is the reason this is not speculative generality. It was chosen as store #2
precisely because it ships no JSON-LD (`docs/store-viability.md`), so phase 5 has to
write a genuinely different `Parser` against this same `Fetcher`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class PageContent:
    """Bytes that arrived, decoded, plus where they actually came from.

    `url` is the final URL after redirects, which is not always the one requested:
    Cyberpuerta answers a category-path URL with a 301 to a flat one.
    """

    url: str
    html: str


@dataclass(frozen=True, slots=True)
class ProductData:
    """One reading of a product page.

    `store` and `external_id` together are the product's identity, matching the
    `(store, external_id)` uniqueness rule in `docs/scraper-design.md`. `canonical_url`
    is only there to show the user a link, and is refreshed on every check — a store
    may rename a slug without the product changing at all, which Cyberpuerta
    demonstrably does.
    """

    store: str
    external_id: str
    canonical_url: str
    name: str
    price_cents: int
    currency: str
    in_stock: bool

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("external_id must not be empty")
        if not self.name:
            raise ValueError("name must not be empty")
        if isinstance(self.price_cents, bool) or not isinstance(self.price_cents, int):
            raise TypeError(f"price_cents must be int cents, got {type(self.price_cents).__name__}")
        if self.price_cents < 0:
            raise ValueError(f"price_cents must not be negative: {self.price_cents}")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"currency must be an ISO 4217 code: {self.currency!r}")


class Fetcher(ABC):
    """How bytes arrive. HTTP today; an official store API would be another one."""

    @abstractmethod
    async def fetch(self, url: str) -> PageContent:
        """Retrieve one page.

        Raises:
            FetchError: or one of its subclasses, which say whether waiting will help.
        """


class Parser(ABC):
    """How bytes become a `ProductData`. Pure, synchronous, one store each."""

    store: ClassVar[str]

    @abstractmethod
    def supports(self, url: str) -> bool:
        """Whether this parser claims the URL. Used to route a link a user pasted."""

    @abstractmethod
    def normalise_url(self, url: str) -> str:
        """Strip a pasted link down to the page worth fetching.

        Store URLs arrive carrying tracking parameters, so the same product reaches us
        in many spellings. This does not produce the `external_id` — for Cyberpuerta
        that id is not in the URL at all, only in the page. See `cyberpuerta.py`.

        Raises:
            UnsupportedUrlError: the URL is not a product page of this store.
        """

    @abstractmethod
    def parse(self, page: PageContent) -> ProductData:
        """Read a product out of a page.

        Raises:
            ProductUnavailableError: the page carries no offer at all.
            LayoutChangedError: the page arrived and we could not read it.
        """
