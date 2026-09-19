"""Tests for what the bot says.

Copy is not usually worth testing — nobody should assert on a sentence. Two things here
are, and neither is about wording:

1. **The error mapping has to stay exhaustive.** Six error types exist precisely so each
   one can become a different reply; a subclass added later with no message of its own
   would silently answer with its parent's, which is the failure the hierarchy was built
   to avoid. Python cannot check that at import time. This file does.
2. **The store list is derived from `PARSERS`.** The moment it is written by hand it
   starts going stale, and it goes stale in the message that tells a user why their link
   was rejected.
"""

from __future__ import annotations

import pytest

from price_tracker.bot import copy
from price_tracker.scrapers import (
    PARSERS,
    FetchError,
    FetchTimeoutError,
    LayoutChangedError,
    PageGoneError,
    ProductUnavailableError,
    RobotsDisallowedError,
    ScraperError,
    StoreRefusedError,
    UnsupportedUrlError,
)


def every_subclass(klass: type) -> list[type]:
    """`klass`'s subclasses, however deeply nested."""
    found = []
    for subclass in klass.__subclasses__():
        found.append(subclass)
        found.extend(every_subclass(subclass))
    return found


#: Only the ones the application ships. `__subclasses__` is global and sees whatever has
#: been imported, so without this filter a throwaway error class defined in another test
#: module would join the list — and which modules pytest has imported by now depends on
#: their filenames, which is not a thing a test should depend on.
SHIPPED_ERRORS = [
    error for error in every_subclass(ScraperError) if error.__module__.startswith("price_tracker.")
]


class FakeParser:
    """Just enough of a parser for the name joining. `_store_names` reads `.store`."""

    def __init__(self, store: str) -> None:
        self.store = store


# ---- the error mapping -------------------------------------------------------


@pytest.mark.parametrize("error_type", SHIPPED_ERRORS, ids=lambda t: t.__name__)
def test_every_scraper_error_has_a_reply_of_its_own(error_type):
    # The guard the hierarchy's docstring promises. Adding a seventh error type without
    # a message fails here rather than in a chat, where it would read as a vaguer
    # version of its parent and nobody would notice it was the wrong answer.
    assert error_type in copy._ERROR_REPLIES, (
        f"{error_type.__name__} has no reply; add one to copy._ERROR_REPLIES"
    )


@pytest.mark.parametrize(
    "error_type",
    [
        UnsupportedUrlError,
        RobotsDisallowedError,
        StoreRefusedError,
        PageGoneError,
        FetchTimeoutError,
        FetchError,
        ProductUnavailableError,
        LayoutChangedError,
    ],
    ids=lambda t: t.__name__,
)
def test_the_most_specific_message_wins(error_type):
    # RobotsDisallowedError, FetchTimeoutError, StoreRefusedError and PageGoneError are
    # all FetchError subclasses. An isinstance chain in the wrong order would hand every
    # one of them the generic message and still pass a test that only checked one.
    assert copy.reply_for_error(error_type("boom")) == copy._ERROR_REPLIES[error_type]


def test_every_reply_is_distinct():
    # Two errors sharing a sentence means one of them is not really being answered.
    messages = list(copy._ERROR_REPLIES.values())
    assert len(set(messages)) == len(messages)


def test_an_unmapped_subclass_inherits_its_parent_s_message():
    class StoreOnFireError(FetchError):
        pass

    # Imprecise but true, which is the right default. It is not the fallback below.
    assert copy.reply_for_error(StoreOnFireError()) == copy._ERROR_REPLIES[FetchError]


def test_a_bare_scraper_error_gets_the_fallback():
    # Nothing raises `ScraperError` itself, so there is nothing useful to say about it.
    assert copy.reply_for_error(ScraperError()) == copy.UNKNOWN_ERROR


# ---- the store list ----------------------------------------------------------


def test_the_store_list_comes_from_the_parser_table():
    for parser in PARSERS:
        assert parser.store.capitalize() in copy.STORES


def test_the_rejection_message_names_the_stores_we_do_read():
    reply = copy.reply_for_error(UnsupportedUrlError("nope"))
    assert copy.STORES in reply


@pytest.mark.parametrize(
    ("stores", "expected"),
    [
        (["cyberpuerta"], "Cyberpuerta"),
        (["cyberpuerta", "liverpool"], "Cyberpuerta y Liverpool"),
        (["liverpool", "cyberpuerta", "amazon"], "Amazon, Cyberpuerta y Liverpool"),
    ],
)
def test_store_names_are_joined_as_spanish_not_as_a_csv(stores, expected):
    # Phase 5 is when the second store arrives; this is the branch that would otherwise
    # run for the first time in front of a user.
    assert copy._store_names([FakeParser(store) for store in stores]) == expected


# ---- the help text -----------------------------------------------------------


@pytest.mark.parametrize("command", ["/add", "/list", "/remove"])
def test_help_documents_every_command_that_exists(command):
    assert command in copy.HELP


def test_the_welcome_message_includes_the_help():
    # `/start` is the only thing most people will ever type. It has to say what to type
    # next, which the phase 0 version — "por ahora solo sé saludar" — did not.
    assert copy.HELP in copy.WELCOME
