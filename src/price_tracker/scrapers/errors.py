"""Failure modes of reading a product page.

These are separate types rather than one exception carrying a string because each one
becomes a different reply to the user in phase 3, and because they are not equally our
fault. The split is by who can act on the failure:

- the user gave us a link we cannot use      -> UnsupportedUrlError
- the store would not hand over the page     -> FetchError and its subclasses
- the store says there is nothing to buy     -> ProductUnavailableError
- the page arrived and we failed to read it  -> LayoutChangedError

That last one is the only one that means *our* code is out of date, which is why it is
worth telling apart from the rest: it is the one that should page the maintainer rather
than apologise to the user.

Returning `None` for all of these was the alternative and it loses exactly this
distinction at the one moment it matters.
"""

from __future__ import annotations


class ScraperError(Exception):
    """Base for every failure of the fetch-and-parse pipeline."""


class UnsupportedUrlError(ScraperError):
    """Not a product URL of a store we support.

    A typo, a category page, a store we deliberately dropped (see
    `docs/store-viability.md`), or a link from a store we never added.
    """


class FetchError(ScraperError):
    """The page could not be retrieved.

    Treated as transient by default: the product stays tracked and the next run tries
    again. Only `consecutive_failures` crossing a threshold retires a product, so one
    bad afternoon at the store does not delete a user's tracking.
    """


class FetchTimeoutError(FetchError):
    """The store did not answer in time."""


class StoreRefusedError(FetchError):
    """The store answered 401, 403 or 429.

    Not retried here. Per `docs/store-viability.md`, a refusal is a finding to record,
    not something to work around: a store that blocks us gets dropped and documented.
    """


class PageGoneError(FetchError):
    """404 or 410 — the product page no longer exists.

    Distinct from the transient failures because it does not get better by waiting, so
    the checker can retire the product instead of accumulating failures forever.
    """


class ProductUnavailableError(ScraperError):
    """The page parsed, but carries no offer at all — nothing to record.

    Note the narrow meaning. A product that is *out of stock but still priced* is not
    this error: it is a perfectly good `ProductData` with `in_stock=False`, and it is
    arguably the most interesting thing to track, since it may come back cheaper. This
    error is only for a page with no price anywhere, where storing a row would mean
    inventing one.
    """


class LayoutChangedError(ScraperError):
    """The page arrived intact and our parser could not read it.

    Means the store changed its markup, or that a page shape exists which the fixtures
    in `tests/fixtures/` do not cover. Either way it is a bug in this repo, not a
    problem with the user's link or with the store.
    """
