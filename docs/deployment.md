# Deployment: Postgres, Docker, CI and Northflank

Phase 6: the phase that decides whether this project goes anywhere. Until now the bot
has run where somebody ran it. The acceptance test for this phase is the one from the
original plan, and it is the right one: **turn the computer off and see whether the
alerts still arrive.**

The first half of this note is what does not depend on where the bot runs — Postgres as
a real backend, the image, and CI. The second half is where it runs — Northflank, with
the database on Supabase — and what the first production deploy found that no test had.

## Postgres is a second test backend, not only a production one

Since phase 2, the models have been written so that SQLite and Postgres cannot disagree:
timestamps go through `UtcDateTime`, enums are a VARCHAR with a CHECK, foreign keys are
switched on explicitly. `docs/database-design.md` lists the traps. But every test ran on
SQLite, so the claim that Postgres behaves the same was exactly that — a claim.

Now the suite runs on either. Set `TEST_DATABASE_URL` to a Postgres database and every
database test uses it; leave it unset and they use in-memory SQLite, as before. CI runs
both, side by side.

**The first Postgres run passed all 359 tests without a single change to a model.**
That is the phase 2 design paying out, and it is worth saying plainly because the
opposite result was entirely plausible. Two things were checked before believing it:

- **That the tests really ran on Postgres.** The test database showed 493 committed
  transactions afterwards, and the same run with a wrong password fails with
  `InvalidPasswordError` rather than falling back to SQLite. A green suite that silently
  ran on the old backend would have proved nothing.
- **That the drift guard can fail on Postgres.** `test_migrations.py` now compares the
  migrated schema to the models on whichever backend is configured. Widening one column
  in the model made it fail with `modify_type … VARCHAR(32)`; reverting it made it pass.
  A guard nobody has seen fail is not yet a guard.

Two details in how it is built:

- **Every test drops the tables before creating them**, not only afterwards. A Postgres
  database outlives the test, so a run killed halfway would otherwise leave the next one
  starting from its rows.
- **The migration test inspects through the async engine.** The project installs only
  async drivers, so there is no synchronous engine to open against Postgres; `run_sync`
  lends the async connection's synchronous face to Alembic's comparison API instead.

## The migration had never run on Postgres

It has now — from the image, against an empty `postgres:17` — and the DDL it produced was
read rather than assumed, because phase 2's worst bug was found exactly that way:

- timestamps are `timestamp with time zone`;
- the status CHECK is over `('active', 'retired')` and the default is `'active'`, so a
  raw INSERT that omits the status passes it — the enum-by-name trap does not come back;
- `telegram_id` is `bigint` and accepts 2⁵²−1, the largest id Telegram documents.

## The URL a host hands out is not the one the engine can open

Every Postgres host, Supabase included, gives out a libpq-style URL:
`postgresql://user:pass@host:5432/db`, sometimes with `?sslmode=require`. Pasted into
this bot unchanged, each half fails somewhere that does not point back at the URL:

- **The scheme names no driver**, so SQLAlchemy picks its default, psycopg2 — which is
  synchronous and not installed. The error is `No module named 'psycopg2'`.
- **SQLAlchemy passes the query string to `asyncpg.connect()` as keyword arguments**,
  and `sslmode` is not one of them. That is a `TypeError` from inside the first
  connection. asyncpg calls the same setting `ssl`, with the same values. This was
  confirmed in the dialect's source rather than remembered: `create_connect_args` does
  `opts.update(url.query)` and nothing else.

`config.async_database_url()` rewrites both on load, and only those. Neither is a guess:
the engine is async, so a URL naming no driver can only mean asyncpg, and `sslmode` and
`ssl` share a vocabulary. A URL that names another driver is left alone. One of the tests
round-trips a password containing `@ / : % ? #`, because a password mangled by the
rewrite fails as an ordinary authentication error — the least helpful way to find out.

## A pooled connection goes stale while the bot is idle

The bot spends hours doing nothing between checks, and a pooler such as Supabase's closes
idle client connections on its own schedule. The connection pool does not know. Without
a check, the first query after a quiet afternoon runs on a dead socket: a `/list` that
fails once and then works, which is the hardest kind of report to act on.

`create_engine` now sets `pool_pre_ping=True` for anything that is not SQLite. It costs
one round trip per checkout — nothing, at this bot's traffic.

## Supabase: the session pooler, because the direct connection is IPv6

Supabase's own connection table, as of this phase:

| Mode | Port | Free plan | Meant for |
|---|---|---|---|
| Direct connection | 5432 | **IPv6 only** | long-lived backends that have IPv6 |
| Shared pooler, session mode | 5432 | IPv4 | persistent backends on IPv4-only networks |
| Shared pooler, transaction mode | 6543 | IPv4 | serverless; **no prepared statements** |

Docker's default network is IPv4, and so is a typical free VM, so the direct connection
is out. Between the two poolers, **session mode**: this is a persistent process, which is
what it is for, and asyncpg uses prepared statements, which transaction mode breaks.

**Will the free project pause?** Supabase pauses a free project after a week of too few
queries; its docs say *"a few user requests to the database each day"* is typically
enough. The checker reads its due list every six hours whether or not anything is due, so
the bot makes at least four queries a day with no users at all. That should be enough,
and it is not verified until a week has passed.

## Supabase's Data API is off, and RLS is on with no policies

Supabase generates a REST API over the `public` schema, and its security advisor flags
every table there without row level security. This bot never uses that API: it connects
to Postgres directly, as the owner of its tables. So the project was created with the
Data API **off** — the door the advisor worries about does not exist — and with
Supabase's automatic RLS **on**, which enables row level security on each new table in
`public` and adds no policies.

RLS with no policies denies everything to every role it applies to, and it does not apply
to the bot: a table's owner bypasses it, and so does a role with `BYPASSRLS`. The bot's
role is both. Checked after the first migration rather than assumed:

| tables | RLS | owner | owner bypasses RLS |
|---|---|---|---|
| `alembic_version`, `price_history`, `products`, `trackings`, `users` | on | `postgres` | yes |

Rejected: **policies**, because there is no client to grant anything to; **an Alembic
migration that enables RLS**, because it is a Postgres-only statement that needs a
dialect guard on SQLite, and it would write one host's concern into the schema history;
**leaving the Data API on**, an unused door on the public internet. The cost of the
choice: the setting lives in the Supabase project, not in the repository. That is where
it belongs, since the Data API that makes it matter is Supabase's too.

## The image

Two stages from the same `python:3.13-slim`. The first has uv and builds the virtualenv;
the second receives only the virtualenv, the migrations and `alembic.ini`. They must
share a base, because a virtualenv points at the interpreter that built it.

- **Dependencies are their own layer**, installed from `uv.lock` before the source is
  copied. An edit to a handler rebuilds in seconds instead of reinstalling matplotlib.
- **`--no-editable`** installs the package itself into the virtualenv, so the runtime
  stage never needs `src/`.
- **Not root.** The bot parses pages from the internet.
- **`PYTHONUNBUFFERED=1`**, or `docker logs` shows nothing until a buffer fills — which
  for a bot that logs a few lines per check can be hours.
- **The font cache is built at build time.** matplotlib builds it on first import, so
  otherwise the first `/chart` after every restart pays several seconds for it.

**`.dockerignore` is an allowlist.** It excludes everything and names the six inputs
the image needs. A denylist has to be remembered each time a new file appears; the
failure it prevents — a `.env` baked into an image — is not recoverable once the image
is pushed. Checked afterwards: the image holds `.venv`, `alembic` and `alembic.ini`,
runs as `bot`, and a search of its filesystem finds no `.env` and no `.db`.

The image is 116 MB compressed. Most of it is numpy, matplotlib and fontTools — about
110 MB of the 247 MB virtualenv — which is what `/chart` costs. There is nothing to trim
short of dropping the feature.

## Migrations are a step, not part of startup

`docker-compose.yml` runs them as a one-shot `migrate` service, and the bot starts only
if it exits successfully. The alternative — `alembic upgrade head` in the bot's own
entrypoint — is common, and wrong under a restart policy: a migration that fails exits
the container, the policy restarts it, and it fails again, forever, with the bot down
the whole time. As a separate step, a failed migration stops the deploy where somebody
can read it, and the running bot is untouched.

The migrations ship inside the image, so any image can bring any database up to its own
schema: `docker run <image> alembic upgrade head`.

## Compose, and port 5433

`docker compose up` starts Postgres, runs the migrations, then the bot. Postgres is
`postgres:17` because that is the major version Supabase runs, so a local run and
production read the same DDL.

The host port is **5433**, which was a note carried over from another project — and on
this machine it is not hypothetical: a native Postgres was listening on 5432 when this
phase started. A container published on 5432 would have been shadowed by it, and the
symptom is a `password authentication failed` that says nothing about ports.

## CI

`.github/workflows/ci.yml`, on every push to `main` and every pull request:

- **lint** — `ruff check` and `ruff format --check`.
- **test (sqlite)** and **test (postgres)** — the whole suite, once per backend, against
  a `postgres:17` service container. Not testcontainers: a service container is plain
  Actions configuration with nothing to install.
- **image** — builds the image, imports the bot inside it, and migrates an empty
  Postgres with it. The URL in that step is written the way a dashboard hands it out,
  `postgresql://`, on purpose: it is the path production will take.
- **publish** — on a push to `main` only, and only once the other three have passed:
  rebuilds the image and pushes it to `ghcr.io/emiliopg13/price-tracker-bot`, tagged
  with the commit hash and with `latest`. This is what production runs; see below.

It reads no secrets. Every job gets `contents: read`; `publish` alone also gets
`packages: write`, and its one credential is the token GitHub issues to every run. The
suite is offline and the one database is a throwaway container, so CI has nothing worth
stealing — which is also why the repository can stay public without thinking about what
a fork's workflow can see. A pull request never runs `publish`.

The whole workflow passed on its first run on GitHub, including the `image` job's
`--network host`, which could not be tried as written on Docker Desktop.

## Where it runs: Northflank

Since phase 4 the checker lives in the polling process, so the host must keep **one
process alive**, at zero cost — not run a job now and then. It is **Northflank's free
Developer Sandbox**, region US Central: services that do not sleep, two services and two
jobs free. Northflank wants a card on file even on the free tier; its forms state *"You
won't be charged while on the Developer Sandbox plan"*, and everything here stays inside
those limits — one service, one job, no addon, the smallest compute plan.

That plan is 0.1 shared vCPU and 256 MB. The image's heaviest work — every module
imported, both 1 MB Liverpool pages parsed, a chart drawn — peaks at 157 MB, and it ran
to completion under exactly that limit before the host was chosen. In production the bot
takes about 18 seconds from container start to polling, which does not matter for a
poller.

Supabase placed the database in us-west-2 when "Americas" was chosen; the bot runs in US
Central. At a few queries per command the distance does not show: an `/add`, store fetch
included, answers in about three seconds.

### Northflank runs an image; it does not build one

Northflank can build from the repository on every push. It does not deploy that way here:

- **Zero cost is certain rather than assumed.** Neither Northflank's pricing page nor its
  billing docs say whether builds are included in the free tier. GitHub Actions and GHCR
  are free for a public repository.
- **Only what passed CI can be deployed.** Northflank's own CI builds every push to
  `main`, green or red. `publish` runs after lint, both test legs and the image checks.
- **The migration and the bot run the same image.** Both are pinned to one commit's tag,
  never `latest`, which moves with every push.

| On Northflank | What it is |
|---|---|
| secret group `price-tracker` | `BOT_TOKEN` and `DATABASE_URL`, as runtime variables, never build arguments |
| job `migrate` | manual; the image with its command overridden to `alembic upgrade head` |
| service `bot` | the image's default command, `price-tracker`; one instance, **no port** — it polls Telegram, and nothing connects to it |

A deploy, in order: push to `main` and wait for `publish`; point `migrate` at the new
commit's tag and run it — if it fails, stop there, and the running bot is untouched; then
point `bot` at the same tag. The first price check runs a minute after the bot starts.
Northflank keeps the logs, so the production compose file with log rotation that this
note once planned is not needed.

### Why not Oracle, which the plan named

The plan named Oracle Cloud's Always Free tier: permanent, and the store measurements
already cleared datacenter IPs. Its own documentation has a clause that bears directly
on a single long-lived process:

> Idle Always Free compute instances may be reclaimed by Oracle. Oracle will deem
> virtual machine and bare metal compute instances as idle if, during a 7-day period,
> the following are true: CPU utilization for the 95th percentile is less than 20%;
> Network utilization is less than 20%; Memory utilization is less than 20% (applies to
> A1 shapes only).
>
> — [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

A bot that long-polls Telegram and reads a handful of pages every six hours meets every
one of those conditions. It is exactly what Oracle calls idle. The consequence would not be an
error; it would be alerts that quietly stop arriving some week after the acceptance test
passed.

Two ways around it circulate, and neither was taken:

- **Burning CPU to look busy** (tools exist for exactly this). Not an option here, for
  the same reason the scrapers do not rotate proxies: it defeats a provider's stated
  policy rather than working within it.
- **Upgrading the account to Pay As You Go**, which is widely reported to exempt
  instances from reclamation while still charging nothing inside the Always Free limits.
  As of 2026-09-22 that exemption is **not** in Oracle's Free Tier FAQ or its Always Free
  documentation, so it is a report, not a fact. It also turns the card from identity
  verification into a card that can be billed.

**Supabase is what makes any host survivable.** The data lives outside it, so a
reclaimed or replaced machine costs a redeploy, not the price history. The host is
disposable; the data is not. Cloud Run with Cloud Scheduler is the fallback if
Northflank stops fitting — webhook mode and the checker as an endpoint, which phase 4
made an entry point rather than a rewrite.

## What the first production deploy found

The migration job failed three times before it passed. None of the three was visible to
the suite, and each error said something more precise than it first appeared to.

1. **A password with symbols broke the migrations — and printed itself.** `alembic/env.py`
   handed the URL to Alembic through `config.set_main_option`. Alembic's config is a
   ConfigParser, where `%` starts an interpolation, and a password with symbols is
   percent-encoded in the URL. It raised before connecting to anything, and the
   ConfigParser error quoted the whole URL into the job's log, password included. The
   password was rotated at once. The URL now goes to the engine directly, and a test runs
   the migrations offline with a percent-encoded password; on the old `env.py` it fails
   with the production error.

   The part worth keeping: `async_database_url()` was already tested with exactly such a
   password (see above). The rewrite was right. The next thing to touch the URL was never
   tested with it. **A value has to be tested through every consumer, not only the one
   that produces it.**
2. **`ssl=` takes one exact word.** asyncpg reads `ssl=require` as an SSL mode, and
   anything else — `Require`, `true`, a trailing space or a stray quote from pasting —
   fails with ``` `sslmode` parameter must be one of … ``` before any connection is
   attempted. Each variant was reproduced locally; the fix was in the secret, not the code.
3. **A wrong password names the right user.** Supabase's pooler answers an unknown
   project with "Tenant or user not found", so `password authentication failed for user
   "postgres"`, read carefully, confirms that the network, TLS through the pooler and the
   project reference are all correct. Only the password was wrong; a second reset, copied
   rather than typed, fixed it.

The third attempt is also where `?ssl=require` through Supabase's pooler, the one link
never tried before the deploy, turned out to work.

## Verified live, 2026-09-24

On Northflank against Supabase, driven from the phone:

```
migrate  Running upgrade  -> 22b08eb64bc5, initial schema          exit 0
bot      price check scheduled every 6:00:00 (limit=25)
         resources ready
         Application started
         price check: nothing due                                   60 s later
/add Kingston A400, target 800         -> tracking cyberpuerta/c156664f… at 80000 cents
/add Liverpool 1100215191, target 700  -> tracking liverpool/1100215191 at 70000 cents
```

**The two `/add`s are also the store measurement from Northflank's IP.** The stores had
been measured from a residential IP and from Azure only, and in phase 0 Walmart's `200`
turned out to be a 13 KB anti-bot page. A parse is a stricter test than a status code or
a body size: the parser fails on an interstitial rather than reading it, and the lines
above are written only after a page has been fetched, parsed and stored. Both stores
served Northflank.

**The acceptance test passed, at the second attempt to run it.** The first was meant to
be the scheduled run at 01:14 UTC, and it was never a test: the SQL that arms it had not
been run, and the run would have found nothing due anyway — the two `/add`s landed three
minutes after the 19:14 run, so at 01:14 they were three minutes short of six hours old.
Reading why turned up a bug in the schedule; see below.

The second was armed by hand in Supabase's SQL editor: both products backdated a day so
they were due, their last price set to a fake $1,500 so the alert would have an
"antes", both targets raised to $1,000, the alert bookkeeping cleared. The service was
restarted, since the first check runs a minute after the bot starts, and the computer
was turned off. At 02:27 UTC both messages arrived on the phone:

```
🎉 ¡Bajó de precio!

Kingston SA400S37/240G SSD 2.5" SATA III
Ahora: $889.00 MXN
Antes: $1,500.00 MXN
Tu objetivo: $1,000.00 MXN
```

and the same for the batidora at $668.00. The database agreed: one new reading per
product, four seconds apart, and each tracking's `last_alerted_at` set to the moment of
its reading. The targets were put back afterwards.

**The empty 01:14 run exposed a bug: a product was checked every twelve hours, not
six.** The job runs every six hours and checks what is at least six hours old. A product
checked by one pass is stamped a few seconds after that pass starts, once its page has
been fetched, so six hours later, when the next pass starts, it is those few seconds
short of due and waits for the pass after. The two numbers were kept equal on purpose —
`jobs.py` said so — and the equality is the bug. The tests asked whether a product was
due one hour later and seven hours later, never exactly one pass later. A product is now
due once it is older than half the period; `docs/price-checker.md` has the reasoning and
the test that asks the missing question.

## Verified locally, 2026-09-22

Against a throwaway database on the local Postgres, migrated by the image, with the real
stores and Telegram replaced by a list:

```
/add Kingston A400, target 800      -> $879.00 MXN
/add Liverpool 1100215191, target 700 -> $668.00 MXN, with the 🎉 line
check run: checked=2 succeeded=2 failed=0 retired=0 alerted=0 crashed=0
/chart 1, /chart 2                  -> two PNGs, captions "2 lecturas"
```

`alerted=0` is the result that matters. The batidora was already under target when it
was added, so the `/add` reply was the alert, and the checker correctly stayed quiet —
which means `last_alerted_at` survived the round trip through `timestamptz` and compared
correctly against an aware `now`. That comparison is the one `UtcDateTime` exists for.

## What phase 6 has not done yet

- The README's final shape.
- A week on Supabase, to see whether four queries a day keep the free project awake.
- Read the first restart's logs. Northflank does not halt the old container until its
  replacement is running, so for a moment two processes poll with one token and Telegram
  should answer one of them with `Conflict`. The acceptance test's restart was the first
  one; its logs have not been read yet.
- Northflank's billing page, a few days in. It should read $0.00.
