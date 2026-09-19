"""Tests for the fetcher, with the network mocked by respx.

Two things are being checked here. The obvious one is that each HTTP outcome becomes
the right exception. The less obvious one is that the README's conduct rules are
actually enforced by this class rather than merely described in it — a `robots.txt`
that disallows a path has to stop the request, not just be logged.
"""

import time

import httpx
import pytest
import respx

from price_tracker.scrapers import (
    FetchError,
    FetchTimeoutError,
    HttpFetcher,
    PageGoneError,
    RobotsDisallowedError,
    StoreRefusedError,
    StoreUnavailableError,
)
from price_tracker.scrapers.http import USER_AGENT

HOST = "https://www.cyberpuerta.mx"
PAGE = f"{HOST}/producto.html"
ROBOTS = f"{HOST}/robots.txt"


@pytest.fixture
def fetcher():
    """A fetcher with the pause disabled, so tests do not sleep for real."""
    _client, fetcher = HttpFetcher.build(min_interval=0.0)
    yield fetcher
    # respx intercepts the transport; the client holds no real connection to close.


def allow_all() -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))


@respx.mock
async def test_reads_a_page(fetcher):
    allow_all()
    respx.get(PAGE).mock(return_value=httpx.Response(200, html="<html>hola</html>"))

    page = await fetcher.fetch(PAGE)

    assert page.html == "<html>hola</html>"
    assert page.url == PAGE


@respx.mock
async def test_identifies_itself_honestly(fetcher):
    allow_all()
    route = respx.get(PAGE).mock(return_value=httpx.Response(200, html="ok"))

    await fetcher.fetch(PAGE)

    sent = route.calls.last.request.headers["user-agent"]
    assert sent == USER_AGENT
    # The rule that does not move: no browser impersonation, and a store operator can
    # find out who we are.
    assert "Mozilla" not in sent
    assert "github.com/EmilioPG13/price-tracker-bot" in sent


@respx.mock
async def test_the_final_url_after_a_redirect_is_the_one_reported(fetcher):
    # Cyberpuerta 301s a category-path URL to a flat one; the parser needs to know
    # where the HTML really came from.
    allow_all()
    respx.get(PAGE).mock(
        return_value=httpx.Response(301, headers={"Location": f"{HOST}/final.html"})
    )
    respx.get(f"{HOST}/final.html").mock(return_value=httpx.Response(200, html="ok"))

    page = await fetcher.fetch(PAGE)

    assert page.url == f"{HOST}/final.html"


@respx.mock
async def test_a_disallowed_path_is_never_requested(fetcher):
    respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /producto.html\n")
    )
    page = respx.get(PAGE).mock(return_value=httpx.Response(200, html="should not happen"))

    with pytest.raises(RobotsDisallowedError):
        await fetcher.fetch(PAGE)

    assert not page.called


@respx.mock
async def test_robots_is_fetched_once_per_host(fetcher):
    robots = respx.get(ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get(url__startswith=HOST).mock(return_value=httpx.Response(200, html="ok"))

    await fetcher.fetch(PAGE)
    await fetcher.fetch(f"{HOST}/otro.html")

    # A price check that re-read robots.txt every time would double our traffic.
    assert robots.call_count == 1


@respx.mock
async def test_a_missing_robots_file_is_not_a_ban(fetcher):
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(PAGE).mock(return_value=httpx.Response(200, html="ok"))

    page = await fetcher.fetch(PAGE)

    assert page.html == "ok"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, StoreRefusedError),
        (401, StoreRefusedError),
        (429, StoreRefusedError),
        (404, PageGoneError),
        (410, PageGoneError),
        (500, StoreUnavailableError),
        (503, StoreUnavailableError),
        (418, FetchError),
    ],
)
@respx.mock
async def test_http_failures_keep_their_meaning(fetcher, status, expected):
    allow_all()
    respx.get(PAGE).mock(return_value=httpx.Response(status))

    with pytest.raises(expected):
        await fetcher.fetch(PAGE)


@respx.mock
async def test_a_5xx_is_told_apart_from_every_other_failure(fetcher):
    """The one distinction the checker's retry policy is built on.

    `StoreUnavailableError` subclasses `FetchError`, so a test asserting the parent
    passes either way — which is exactly how this could be collapsed back into a generic
    `FetchError` without anything going red, and the retry would silently stop
    happening. `type(...) is` rather than `isinstance` is the whole point of this test.
    """
    allow_all()
    respx.get(PAGE).mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError) as raised:
        await fetcher.fetch(PAGE)

    assert type(raised.value) is StoreUnavailableError


@respx.mock
async def test_a_refusal_is_not_retried(fetcher):
    allow_all()
    route = respx.get(PAGE).mock(return_value=httpx.Response(403))

    with pytest.raises(StoreRefusedError):
        await fetcher.fetch(PAGE)

    # A 403 asked twice is just hammering. Retrying on 5xx is the checker's job.
    assert route.call_count == 1


@respx.mock
async def test_a_timeout_says_so(fetcher):
    allow_all()
    respx.get(PAGE).mock(side_effect=httpx.ConnectTimeout("too slow"))

    with pytest.raises(FetchTimeoutError):
        await fetcher.fetch(PAGE)


@respx.mock
async def test_a_transport_failure_is_a_fetch_error(fetcher):
    allow_all()
    respx.get(PAGE).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(FetchError):
        await fetcher.fetch(PAGE)


@respx.mock
async def test_requests_to_one_host_are_spaced_out():
    client, fetcher = HttpFetcher.build(min_interval=0.05)
    allow_all()
    respx.get(url__startswith=HOST).mock(return_value=httpx.Response(200, html="ok"))

    started = time.monotonic()
    await fetcher.fetch(PAGE)
    await fetcher.fetch(f"{HOST}/otro.html")
    elapsed = time.monotonic() - started

    assert elapsed >= 0.05
    await client.aclose()
