"""The HTTP half of a scraper, and the place the conduct rules are enforced.

The README states them in prose under "Scraping conduct": an honest `User-Agent` naming
the project, `robots.txt` respected, one request per page, pauses between requests,
retry only on 5xx. Prose is not enforcement, so they live here as code:

- the `User-Agent` is the project and its URL, and there is no way to pass a browser
  string through this class,
- `robots.txt` is fetched once per host and cached, and a disallowed path is never
  requested,
- requests to one host are spaced by `min_interval`, honouring a `Crawl-delay` if the
  store asks for a longer one,
- nothing is retried here at all. A 403 answered twice is just hammering, and the
  retry-on-5xx rule belongs to the scheduled checker in `bot/checker.py`, which is the
  layer that knows how long it may wait. What this layer owes that one is a 5xx it can
  *recognise*, which is why `StoreUnavailableError` is a type of its own.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from price_tracker.scrapers.base import Fetcher, PageContent
from price_tracker.scrapers.errors import (
    FetchError,
    FetchTimeoutError,
    PageGoneError,
    StoreRefusedError,
    StoreUnavailableError,
)

# Truthful identification. A store operator who wants to refuse this bot should be able
# to, and should be able to find out who to complain to.
USER_AGENT = "price-tracker-bot/0.1 (+https://github.com/EmilioPG13/price-tracker-bot)"

DEFAULT_TIMEOUT = httpx.Timeout(20.0)
DEFAULT_MIN_INTERVAL = 2.0  # seconds between requests to the same host


class RobotsDisallowedError(FetchError):
    """`robots.txt` forbids this path. Not fetched, and not retried."""


class HttpFetcher(Fetcher):
    """Fetches product pages over HTTP, politely.

    Holds per-host state (the parsed `robots.txt`, the time of the last request), so
    one instance should be shared across a run rather than created per product — that
    is what makes the spacing and the robots cache mean anything.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        obey_robots: bool = True,
    ) -> None:
        self._client = client
        self._min_interval = min_interval
        self._obey_robots = obey_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def build(
        cls,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        obey_robots: bool = True,
    ) -> tuple[httpx.AsyncClient, HttpFetcher]:
        """Create a fetcher with its own client. The caller owns and closes the client."""
        client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-MX,es;q=0.9",
            },
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        return client, cls(client, min_interval=min_interval, obey_robots=obey_robots)

    async def fetch(self, url: str) -> PageContent:
        host = urlparse(url).netloc

        if self._obey_robots and not await self._allowed(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")

        await self._wait_turn(host)

        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(f"timed out fetching {url}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} fetching {url}: {exc}") from exc

        status = response.status_code
        if status in (401, 403, 429):
            raise StoreRefusedError(f"store refused with HTTP {status}: {url}")
        if status in (404, 410):
            raise PageGoneError(f"page is gone (HTTP {status}): {url}")
        # 5xx before the general case: this is the only status the checker retries, and
        # it can only tell it apart if the type says so. Collapsing it into `FetchError`
        # would make a store's bad minute indistinguishable from a DNS failure.
        if status >= 500:
            raise StoreUnavailableError(f"store is unavailable (HTTP {status}): {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} fetching {url}")

        # `response.url` is the end of the redirect chain, which is where the HTML
        # actually came from; the parser needs that, not what we asked for.
        return PageContent(url=str(response.url), html=response.text)

    async def _wait_turn(self, host: str) -> None:
        """Space out requests to one host, without serialising different hosts.

        The slot is reserved under the lock and slept on outside it. Sleeping while
        holding the lock would make a pause for one store delay every other store, and
        would let two concurrent calls for the same host both wake to the same slot.
        """
        now = time.monotonic()
        async with self._lock:
            interval = max(self._min_interval, self._crawl_delay(host))
            last = self._last_request_at.get(host)
            slot = now if last is None else max(now, last + interval)
            self._last_request_at[host] = slot

        if (wait := slot - now) > 0:
            await asyncio.sleep(wait)

    def _crawl_delay(self, host: str) -> float:
        parser = self._robots.get(host)
        if parser is None:
            return 0.0
        delay = parser.crawl_delay(USER_AGENT)
        return float(delay) if delay else 0.0

    async def _allowed(self, url: str) -> bool:
        parts = urlparse(url)
        parser = await self._robots_for(parts.scheme, parts.netloc)
        # No usable robots.txt: absence is not permission, but it is not a ban either,
        # and every store measured in the spike served one.
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    async def _robots_for(self, scheme: str, host: str) -> RobotFileParser | None:
        if host in self._robots:
            return self._robots[host]

        parser: RobotFileParser | None = None
        try:
            response = await self._client.get(f"{scheme}://{host}/robots.txt")
            if response.status_code == 200:
                parser = RobotFileParser()
                # Deliberately not `parser.read()`: that opens its own blocking
                # connection with urllib, inside an async function.
                parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            parser = None

        self._robots[host] = parser
        return parser
