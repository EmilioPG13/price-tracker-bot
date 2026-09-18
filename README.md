# price-tracker-bot

Telegram bot that tracks product prices in online stores, keeps their history, and
alerts you when a price drops below your target.

> **Status: phase 2 done.** The scraper reads a name and price from a Cyberpuerta
> product page, and there is a schema to keep them in. No bot commands yet.
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

## Running it

```bash
uv sync
cp .env.example .env    # then fill in BOT_TOKEN from @BotFather

uv run alembic upgrade head
uv run price-tracker    # /start responds

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
project and links to it, and `robots.txt` honoured before fetching. No rotating
proxies, no captcha solving, no browser impersonation. A store that blocks this bot
gets dropped from the MVP and written down in `docs/store-viability.md`.
