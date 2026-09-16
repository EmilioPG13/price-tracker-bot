"""Measure whether a store lets us read a price, and from which networks.

This is a throwaway measurement tool, deliberately standalone: it predates the
scraper abstractions and must not grow a dependency on them. Its only job is to
produce evidence for docs/store-viability.md so the store and hosting choices are
made from data instead of optimism.

The same script runs from a laptop (residential IP) and from GitHub Actions
(datacenter IP). Comparing the two runs is the whole point: a store that answers at
home and 403s from CI cannot be checked by a free cloud cron.

Usage:
    uv run python scripts/store_spike.py --label home
    uv run python scripts/store_spike.py --label github-actions --out spike-results/ci.json

Politeness: exactly one request per product page plus one robots.txt per host, with
a pause between stores. A 403 is a result to record, never something to retry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.lexbor import LexborHTMLParser

REPO_ROOT = Path(__file__).resolve().parent.parent

# Truthful identification. We are not pretending to be a browser: if a store wants to
# refuse this bot, it should be able to, and a human should be able to find us.
USER_AGENT = "price-tracker-bot/0.1 (+https://github.com/emiliopg/price-tracker-bot; spike)"

TIMEOUT = httpx.Timeout(20.0)
PAUSE_RANGE = (2.0, 4.0)  # seconds between stores


@dataclass
class StoreResult:
    name: str
    url: str
    status: int | None = None
    elapsed_ms: int | None = None
    bytes_received: int | None = None
    robots_status: int | None = None
    robots_allows: bool | None = None
    has_jsonld_product: bool = False
    parsed_name: str | None = None
    parsed_price: str | None = None
    parsed_currency: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        if self.status is None:
            return "SKIPPED"
        if self.status in (401, 403, 429):
            return "BLOCKED"
        if self.status >= 400:
            return f"HTTP {self.status}"
        if self.parsed_price:
            return "OK (price read)"
        if self.status == 200:
            return "SERVED, no price"
        return f"HTTP {self.status}"


def extract_jsonld_product(html: str) -> tuple[bool, str | None, str | None, str | None]:
    """Look for a schema.org Product in JSON-LD.

    JSON-LD is tried before CSS selectors because it is a semi-formal contract a store
    breaks less often than its class names. Returns (found_product, name, price, currency).
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

        # A page may ship a bare object, a list, or an @graph wrapper.
        candidates: list[dict] = []
        if isinstance(data, dict):
            candidates = data.get("@graph") if isinstance(data.get("@graph"), list) else [data]
        elif isinstance(data, list):
            candidates = [d for d in data if isinstance(d, dict)]

        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("@type")
            types = entry_type if isinstance(entry_type, list) else [entry_type]
            if "Product" not in types:
                continue

            name = entry.get("name")
            offers = entry.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            price = currency = None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                currency = offers.get("priceCurrency")
            return (
                True,
                str(name) if name else None,
                str(price) if price is not None else None,
                str(currency) if currency else None,
            )
    return (False, None, None, None)


async def check_robots(client: httpx.AsyncClient, url: str) -> tuple[int | None, bool | None]:
    """Fetch and evaluate robots.txt for the product URL's host."""
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url)
    except httpx.HTTPError:
        return None, None

    if resp.status_code != 200:
        # No usable robots.txt. Absence is not permission, but it is not a ban either.
        return resp.status_code, None

    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return resp.status_code, parser.can_fetch(USER_AGENT, url)


async def probe_store(client: httpx.AsyncClient, name: str, url: str) -> StoreResult:
    result = StoreResult(name=name, url=url)

    result.robots_status, result.robots_allows = await check_robots(client, url)
    if result.robots_allows is False:
        result.notes.append("robots.txt disallows this path; not fetched")
        return result

    started = time.perf_counter()
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    result.status = resp.status_code
    result.bytes_received = len(resp.content)

    if resp.status_code != 200:
        # Deliberately no retry: a 403 answered twice is just hammering.
        return result

    found, prod_name, price, currency = extract_jsonld_product(resp.text)
    result.has_jsonld_product = found
    result.parsed_name = prod_name
    result.parsed_price = price
    result.parsed_currency = currency

    if not found:
        result.notes.append("no JSON-LD Product; would need CSS selectors")
    elif not price:
        result.notes.append("JSON-LD Product present but no price in offers")

    return result


async def public_ip_info(client: httpx.AsyncClient) -> str:
    """Best effort: which network did this run measure from."""
    try:
        resp = await client.get("https://ipinfo.io/json")
        if resp.status_code == 200:
            data = resp.json()
            return f"{data.get('ip', '?')} ({data.get('org', 'unknown org')})"
    except (httpx.HTTPError, ValueError):
        pass
    return "unknown"


def render_markdown(label: str, ip_info: str, results: list[StoreResult]) -> str:
    lines = [
        f"### Spike run: `{label}`",
        "",
        f"- Egress: {ip_info}",
        f"- User-Agent: `{USER_AGENT}`",
        "",
        "| Store | Verdict | HTTP | robots | JSON-LD Product | Price read | ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        robots = (
            "n/a" if r.robots_allows is None else ("allows" if r.robots_allows else "**disallows**")
        )
        price = f"{r.parsed_price} {r.parsed_currency or ''}".strip() if r.parsed_price else "—"
        lines.append(
            f"| {r.name} | {r.verdict} | {r.status or '—'} | {robots} "
            f"| {'yes' if r.has_jsonld_product else 'no'} | {price} | {r.elapsed_ms or '—'} |"
        )
    notes = [(r.name, n) for r in results for n in r.notes]
    errors = [(r.name, r.error) for r in results if r.error]
    if notes or errors:
        lines += ["", "**Notes**", ""]
        lines += [f"- `{name}`: {note}" for name, note in notes]
        lines += [f"- `{name}`: {err}" for name, err in errors]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", type=Path, default=REPO_ROOT / "spike" / "targets.toml")
    ap.add_argument("--label", default="local", help="Name this run, e.g. home / github-actions")
    ap.add_argument("--out", type=Path, default=None, help="Optional JSON output path")
    args = ap.parse_args()

    if not args.targets.exists():
        print(f"targets file not found: {args.targets}", file=sys.stderr)
        return 2

    config = tomllib.loads(args.targets.read_text(encoding="utf-8"))
    stores = [s for s in config.get("store", []) if s.get("url")]
    skipped = [s["name"] for s in config.get("store", []) if not s.get("url")]

    if not stores:
        print(
            "No target URLs configured. Add at least one product URL to "
            f"{args.targets} and run again.",
            file=sys.stderr,
        )
        return 2

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-MX,es;q=0.9",
    }
    results: list[StoreResult] = []
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT, follow_redirects=True) as client:
        ip_info = await public_ip_info(client)
        for i, store in enumerate(stores):
            if i:
                await asyncio.sleep(random.uniform(*PAUSE_RANGE))
            print(f"probing {store['name']}...", file=sys.stderr)
            results.append(await probe_store(client, store["name"], store["url"]))

    report = render_markdown(args.label, ip_info, results)
    if skipped:
        report += f"\n\nSkipped (no URL configured): {', '.join(skipped)}"
    print(report)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {"label": args.label, "egress": ip_info, "results": [asdict(r) for r in results]},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}", file=sys.stderr)

    # The spike never fails the build: a block is a finding, not an error.
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
