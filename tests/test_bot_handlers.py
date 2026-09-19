"""Tests for the Telegram side: the wiring, and the one piece of logic in it.

The handlers themselves are four lines each and are covered by `test_bot_commands.py`
from the other side of the seam. What is worth testing here is what the seam does not
cover: that every command is actually registered — a handler nobody wired up is a
command that silently does not exist — and that a reply too long for Telegram is split
rather than rejected.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from price_tracker.bot import copy, jobs
from price_tracker.bot.handlers import MESSAGE_LIMIT, split_message
from price_tracker.bot.runtime import RESOURCES_KEY, Resources, resources_of
from price_tracker.config import Settings
from price_tracker.main import build_application

EXPECTED_COMMANDS = {"start", "help", "add", "list", "chart", "remove"}


@pytest.fixture
def application():
    # A well-formed but fake token. Building an Application opens no socket; only
    # `run_polling` does, and nothing here calls it.
    return build_application(Settings(bot_token="123456:TEST-TOKEN-NOT-A-REAL-ONE"))


# ---- wiring ------------------------------------------------------------------


def test_every_command_is_registered(application):
    registered = {
        command
        for handler in application.handlers[0]
        for command in getattr(handler, "commands", ())
    }

    assert registered == EXPECTED_COMMANDS


def test_the_command_menu_matches_the_handler_table(application):
    """The menu Telegram shows and the commands that exist must be the same set.

    These are written in two files — `copy.COMMAND_MENU` and the table in `main.py` —
    and nothing connects them, so they drift the moment a command is added to one and
    forgotten in the other. The drift is invisible from the code and shows up as a menu
    entry that does nothing, or a working command nobody can discover.

    `/start` is the exception: it is what Telegram sends before there is a menu to read.
    """
    registered = {
        command
        for handler in application.handlers[0]
        for command in getattr(handler, "commands", ())
    }
    advertised = {command for command, _description in copy.COMMAND_MENU}

    assert advertised == registered - {"start"}


def test_the_price_check_is_scheduled(application):
    """A bot that answers commands and never alerts anyone is phase 3 with extra files.

    Registered at build time rather than in `post_init`, so it can be asserted without
    opening anything — the same reason `build_application` is separate from `run`.
    """
    scheduled = application.job_queue.get_jobs_by_name(jobs.JOB_NAME)

    assert len(scheduled) == 1
    interval, limit = scheduled[0].data
    assert interval == timedelta(hours=6)
    assert limit == 25


def test_the_update_log_runs_before_the_handlers(application):
    # Group -1 is the whole point: it sees the updates no command claims, which is the
    # case it was added for. In group 0 it would answer a different question.
    assert -1 in application.handlers
    assert len(application.handlers[-1]) == 1


def test_an_error_handler_is_registered(application):
    # Without one, an unexpected exception is logged and the chat stays silent, so the
    # user retries a command that will fail again.
    assert application.error_handlers


def test_resources_are_not_opened_until_post_init(application):
    # Building the Application must touch neither the database nor the network: both
    # bind to the event loop, which `run_polling` has not started yet.
    assert RESOURCES_KEY not in application.bot_data


def test_a_handler_without_resources_says_what_is_missing():
    with pytest.raises(RuntimeError, match="post_init"):
        resources_of({})


def test_resources_of_finds_what_post_init_left(engine):
    from price_tracker.db import create_session_factory
    from price_tracker.scrapers import HttpFetcher

    _, fetcher = HttpFetcher.build()
    resources = Resources(engine=engine, sessions=create_session_factory(engine), fetcher=fetcher)

    assert resources_of({RESOURCES_KEY: resources}) is resources


# ---- splitting a long reply --------------------------------------------------


def test_a_short_message_is_left_alone():
    assert split_message("hola") == ["hola"]


def test_a_long_list_is_split_between_products_not_inside_one():
    entry = "1. Un producto\n   Ahora: $1.00 MXN\n   https://example.com"
    text = "\n\n".join([entry] * 200)

    chunks = split_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)
    # The indented lines are a product's price and its link. A chunk starting on one of
    # them would open with a price and no name, which is the failure worth preventing.
    for chunk in chunks:
        assert not chunk.startswith("   ")


def test_splitting_loses_nothing():
    text = "\n\n".join(f"{number}. line\n   detalle" for number in range(600))

    # Content is preserved exactly; only the separators at the boundaries are consumed,
    # since each chunk becomes its own message.
    assert "".join(split_message(text)).replace("\n", "") == text.replace("\n", "")


def test_a_single_line_longer_than_the_limit_is_cut():
    # A store with an absurd product name. There is no boundary left to respect, and
    # cutting it is still better than Telegram rejecting the whole message.
    text = "x" * (MESSAGE_LIMIT * 2 + 7)

    chunks = split_message(text)

    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_a_long_line_between_short_ones_keeps_everything_in_order():
    text = "antes\n" + "y" * (MESSAGE_LIMIT + 10) + "\ndespués"

    chunks = split_message(text)

    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)
    assert chunks[0] == "antes"
    assert chunks[-1].endswith("después")
