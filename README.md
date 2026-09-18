# price-tracker-bot

Telegram bot that tracks product prices in online stores, keeps their history, and
alerts you when a price drops below your target.

> **Status: phase 0 done.** Scaffold and store-viability spike measured. No tracking yet.
>
> Stores chosen by measurement: **Cyberpuerta** (phase 1) and **Liverpool** (phase 5).
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

## Running it

```bash
uv sync
cp .env.example .env    # then fill in BOT_TOKEN from @BotFather

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
