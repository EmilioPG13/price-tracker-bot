# price-tracker-bot

Telegram bot that tracks product prices in online stores, keeps their history, and
alerts you when a price drops below your target.

> **Status: phase 4 done.** Paste a product link and a target price, and the bot reads
> the page, keeps the product's history, re-checks it on a schedule and messages you
> when the price crosses your target. `/chart` draws what it has seen. Deployment is
> phase 6; for now it runs where you run it.
>
> Stores chosen by measurement: **Cyberpuerta** (done) and **Liverpool** (phase 5).
> Walmart blocks datacenter IPs, Amazon's terms forbid scraping, Mercado Libre does not
> put prices in its server HTML. Full evidence and reasoning in
> [`docs/store-viability.md`](docs/store-viability.md).

## Why the spike comes first

A price tracker is only worth building if the stores answer. Two facts make that a
real constraint:

- Mercado Libre's API returns `403` to anonymous traffic and requires a registered
  OAuth application.
- Its storefront blocks datacenter IP ranges — which is what every free hosting
  tier hands out.

So before any scraper is written, [`scripts/store_spike.py`](scripts/store_spike.py)
measures each candidate store from two networks: a residential one and a GitHub
Actions runner (a datacenter IP, free and unlimited on a public repo). The results
in [`docs/store-viability.md`](docs/store-viability.md) decide which stores make the
MVP and where the bot can run.

## The scraper

`Fetcher` (how bytes arrive) and `Parser` (how bytes become a product) are separate, so
a store changing its HTML and a store blocking our requests are two different repairs.
Parsing is synchronous and I/O-free, which is why the whole suite runs offline against
real pages saved in [`tests/fixtures/`](tests/fixtures/).

Two decisions are worth reading before the code, both made against the obvious option
and both settled by looking at real pages — a product's identity is not in its URL, and
a store's `sku` is not the store's id for it:
[`docs/scraper-design.md`](docs/scraper-design.md).

## Storage

Four tables — users, products, trackings, price history — on SQLAlchemy 2 async with
Alembic migrations. SQLite locally, Postgres in production; most of the design is about
the places those two disagree quietly, such as a foreign key that is declared and not
enforced, or a timestamp that loses its timezone on one backend only.

A product is keyed on `(store, external_id)` rather than its URL, money is integer cents
in every column, and the alert rule lives on the tracking row so the part everyone
forgets — re-arming after the price recovers — cannot be omitted by a caller:
[`docs/database-design.md`](docs/database-design.md).

## The bot

```
/add <link> <precio>    track a product, and say what price is worth hearing about
/list                   what you track, numbered
/chart <número>         the price history of the one with that number, as a picture
/remove <número>        stop tracking the one with that number
```

Plus the half you do not type: every few hours the bot re-reads every product that has
gone stale and messages whoever asked to be told when one crosses their target.

A command returns text and imports no `telegram`; the PTB handlers are four-line
adapters around it. The scheduled checker comes through that same seam — it has no chat
to reply to, so it takes a "send this to that user" callable instead. Both halves go
through one scraper function and one repository, which is what stops the two runtimes
growing different ideas of what recording a price means. The suite exercises the real
parser, the real repository and the real alert rule without constructing a single
`Update`, and replaces Telegram with a list.

Each of the scraper's error types becomes its own reply, which is what that hierarchy
was built for. `/add` reads a live page, so it records the price and applies it to the
alert rule: if the product is already under your target, the reply *is* the alert, and
the checker does not repeat it later.
[`docs/bot-commands.md`](docs/bot-commands.md) and
[`docs/price-checker.md`](docs/price-checker.md).

## Running it

```bash
uv sync
cp .env.example .env    # then fill in BOT_TOKEN from @BotFather

uv run alembic upgrade head
uv run price-tracker    # then /start in the chat

uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Run the spike:

```bash
# Add product URLs to spike/targets.toml first.
uv run python scripts/store_spike.py --label home --out spike-results/home.json

# Same script, datacenter IP:
gh workflow run store-spike.yml
```

## Scraping conduct

One request per product page, pauses between stores, a `User-Agent` that names this
project and links to it, and `robots.txt` honoured before fetching. The only failure
retried is a 5xx — the store saying it is broken. A refusal is never retried: it is a
finding to record, not an obstacle to work around. No rotating proxies, no captcha
solving, no browser impersonation. A store that blocks this bot gets dropped from the
MVP and written down in `docs/store-viability.md`.

None of that is only prose. `HttpFetcher` has no way to send a browser string, a test
asserts a `robots.txt` disallow means the product request is never made, and another
counts the requests after a 403 to prove there was exactly one.
