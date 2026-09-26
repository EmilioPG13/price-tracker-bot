# price-tracker-bot

A Telegram bot that watches product prices in Mexican online stores and messages you when
one drops to the price you asked for.

<!--
  15-second GIF of the bot in use: /add → reply → /chart. Save it as docs/demo.gif and
  replace this comment with:
  <p align="center"><img src="docs/demo.gif" width="320" alt="Adding a product to the bot and asking for its price chart"></p>
-->

**Python 3.13** · python-telegram-bot · SQLAlchemy 2 (async) + Alembic · PostgreSQL on
Supabase · Docker · GitHub Actions · Northflank

Deployed and running since September 2026, at zero cost: Northflank runs the image CI
publishes, against Postgres on Supabase. It passed the test the project was built
against — computer off, and the price alerts still arrived.

## Try it

**[t.me/Emilio_price_tracker_bot](https://t.me/Emilio_price_tracker_bot)**, then `/start`.
The bot speaks Spanish.

```
/add <link> <precio>    track a product, and say what price is worth hearing about
/list                   what you track, numbered
/chart <número>         the price history of the one with that number, as a picture
/remove <número>        stop tracking the one with that number
```

It reads links from **Cyberpuerta** and **Liverpool**. Every six hours it re-reads what
people track and writes to whoever asked to hear about a price that has reached their
target. Up to 10 products per person.

**What it keeps about you:** your Telegram id and the products you track. Its logs also
record each message you send it, with your username, because that is how a problem gets
diagnosed.

## How it works

```mermaid
flowchart LR
    you(["You, in Telegram"]) <--> api["Telegram Bot API"]

    subgraph host ["Northflank: one container"]
        direction TB
        handlers["handlers.py<br/>PTB adapters"] --> commands["commands.py<br/>/add /list /chart /remove"]
        jobs["jobs.py<br/>every 6 hours"] --> checker["checker.py"]
        commands --> fetch["fetch_product()"]
        checker --> fetch
        commands --> repo["Repository"]
        checker --> repo
    end

    api <-->|long polling| handlers
    jobs -.->|price alerts| api
    fetch -->|one request per page| stores[("Cyberpuerta<br/>Liverpool")]
    repo --> db[("PostgreSQL<br/>Supabase")]
```

**Two callers, one service layer.** A command is a function that returns text and
imports nothing from `telegram`; the handlers are four-line adapters around it. The
scheduled checker comes through the same seam. It has no chat to reply to, so it takes a
"send this to that user" callable instead. Both go through one scraper function and one
repository, so the two can never disagree about what recording a price means. That is
also why the tests reach everything behind the adapters without building a single
Telegram `Update`.

**From commit to production:** a push to `main` runs lint, the test suite on SQLite *and*
Postgres, and checks on the Docker image. Only when all of that is green does CI publish
the image to GHCR, tagged with the commit. A deploy points Northflank's migration job at
that tag, runs it, and then points the bot at the same tag. Details in
[`docs/deployment.md`](docs/deployment.md).

## Decisions, and why

Each part of the system has a note in [`docs/`](docs/) with the reasoning behind it. The
short version:

- **Stores were chosen by measurement.** From a datacenter IP all five candidates answered
  `200`, and two of those answers were lies. Walmart's was a 13 KB anti-bot page. Comparing
  body sizes from two networks gave it away.
  [store-viability](docs/store-viability.md)
- **Structured data before CSS selectors.** Cyberpuerta is read from its `schema.org`
  JSON-LD. Liverpool publishes none, so it is read from the state its Next.js pages stream
  inline, behind the same `fetch_product()`. Adding that store took two new files and one
  line of the parser table.
  [scraper-design](docs/scraper-design.md), [liverpool-parser](docs/liverpool-parser.md)
- **A product is the store's own id, not its URL or its SKU.** One SSD arrived as three
  different URLs in a single page load, and the `sku` turned out to be the manufacturer's
  part number, so two listings of the same part would collide on it.
  [scraper-design](docs/scraper-design.md)
- **The price you pay is `promoPrice`, not `salePrice`.** On the first page captured all
  three price fields were equal. Only a second, discounted page showed that `salePrice` is
  the price before the discount. There is no fallback, because a failed read beats a
  plausible wrong number. [liverpool-parser](docs/liverpool-parser.md)
- **One product row, however many people watch it,** so each product costs one request
  per check. Money is integer cents in every column, because `Numeric` on SQLite round-trips
  through float. [database-design](docs/database-design.md)
- **The alert rule lives on the tracking row**, including the part everyone forgets:
  re-arming once the price goes back up. A caller cannot skip it.
  [database-design](docs/database-design.md)
- **The checker is a job inside the bot's process, not a cron job.** A bot that polls is
  never idle, so a host that sleeps idle processes would break it anyway. One process,
  one thing to deploy. [price-checker](docs/price-checker.md)
- **Alerts are committed before they are sent.** This is a trade, not a solution. A failed
  send means one missed alert, while the other order makes the bot repeat itself.
  [price-checker](docs/price-checker.md)
- **Retry only on 5xx.** A 5xx is the store saying it is broken, so waiting is the polite
  response. A 403 is a refusal: it is written down, never retried.
  [price-checker](docs/price-checker.md)
- **The whole suite runs on SQLite and on Postgres.** The two disagree without saying so about
  cascades, time zones and where NULL sorts. The second backend in CI turns "the models
  handle it" from a claim into a check. [deployment](docs/deployment.md)
- **Migrations are a deploy step, not part of startup.** A failed migration stops the
  deploy where it can be read, instead of crash-looping the bot.
  [deployment](docs/deployment.md)
- **10 products per person, and a product nobody tracks is never re-read.** The cap alone
  could be walked around by adding and removing products.
  [bot-commands](docs/bot-commands.md)

## Run it locally

You need a bot token of your own from [@BotFather](https://t.me/BotFather). Only one
process can poll a token at a time.

```bash
cp .env.example .env         # set BOT_TOKEN
docker compose up --build    # Postgres on host port 5433, migrations, then the bot
```

Or without Docker, on SQLite, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env         # set BOT_TOKEN
uv run alembic upgrade head
uv run price-tracker
```

## Tests

```bash
uv run pytest                # 360+ tests, about 20 seconds
uv run pytest --cov          # with coverage, as CI runs it
uv run ruff check . && uv run ruff format --check .
```

- **No test touches the network.** The store pages in [`tests/fixtures/`](tests/fixtures/)
  are real captures, saved byte for byte. The fetcher is a stub that serves them, and
  Telegram is replaced by a list.
- **91% line coverage, and CI fails below 88%.** Nearly all of what is not covered is the
  Telegram adapters (`handlers.py`, `jobs.py`, `runtime.py`). They are kept thin so that
  everything behind them can be tested without Telegram, and they were verified by hand
  from a phone.
- **CI runs the suite twice**, on SQLite and on Postgres 17. To run it on Postgres
  locally, point it at a database it is allowed to wipe:

  ```bash
  docker compose up -d db
  docker compose exec db createdb -U price_tracker price_tracker_test
  TEST_DATABASE_URL=postgresql://price_tracker:price_tracker@127.0.0.1:5433/price_tracker_test uv run pytest
  ```

## Honest limitations

- **Two stores.** Walmart blocks datacenter IPs behind a fake `200`. Amazon's conditions
  of use forbid scraping. Mercado Libre does not put its prices in the server HTML, and its
  API requires a registered app. [store-viability](docs/store-viability.md)
- **Every six hours, and at most 25 products per pass**, stalest first. With more distinct
  products than that, every product gets checked less often. Nobody is skipped, everybody
  waits longer. The per-person cap bounds how much one person can slow everyone else down.
- **A store redesign breaks its parser.** The bot then tells you the store changed its
  page and logs the failure; it does not guess at a price.
- **Free tiers, one process, no monitoring.** Northflank says its free sandbox is not for
  production. Supabase pauses free projects that go quiet; the checker's queries every six
  hours should keep it awake, but that is an expectation, not a guarantee. Nothing alerts
  anyone if the bot stops.
- **Mexican pesos only.** Liverpool never states a currency for its own products, so
  pesos are assumed. A check fails the read if a page declares currencies and pesos are
  not among them.
- **No way to erase your data from the chat.** `/remove` stops tracking a product. Your
  Telegram id stays in the database.

## Scraping conduct

One request per product page, pauses between requests to the same store, a `User-Agent`
that names this project and links to it, and `robots.txt` honoured before any fetch. The
only failure retried is a 5xx. No rotating proxies, no captcha solving, no browser
impersonation. A store that blocks this bot is dropped and documented in
[`docs/store-viability.md`](docs/store-viability.md).

None of that is only prose. `HttpFetcher` has no way to send a browser `User-Agent`, one
test asserts that a `robots.txt` disallow means the product page is never requested, and
another counts the requests after a 403 to prove there was exactly one.
