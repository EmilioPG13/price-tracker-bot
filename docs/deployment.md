# Deployment: Postgres, Docker and CI

Phase 6: the phase that decides whether this project goes anywhere. Until now the bot
has run where somebody ran it. The acceptance test for this phase is the one from the
original plan, and it is the right one: **turn the computer off and see whether the
alerts still arrive.**

This note covers the half that does not depend on where the bot ends up — Postgres as a
real backend, the image, and CI. The host is still open, and the last section says why
that turned out to be a harder question than the plan assumed.

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

It reads no secrets and asks for `contents: read` only. The suite is offline and the one
database is a throwaway container, so CI has nothing worth stealing — which is also why
the repository can stay public without thinking about what a fork's workflow can see.

The image job and the Postgres setup were reproduced locally before being written down.
**The workflow itself has not run on GitHub yet**; that happens with the push.

## Where it runs: still open, and harder than the plan assumed

The plan named Oracle Cloud's Always Free tier: permanent, and the store measurements
already cleared datacenter IPs. Phase 4 then made the requirement stricter — the checker
lives in the polling process, so the host must keep **one process alive**, not run a job
now and then.

Oracle's own documentation has a clause that bears directly on that:

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

Two ways around it circulate, and neither is taken yet:

- **Burning CPU to look busy** (tools exist for exactly this). Not an option here, for
  the same reason the scrapers do not rotate proxies: it defeats a provider's stated
  policy rather than working within it.
- **Upgrading the account to Pay As You Go**, which is widely reported to exempt
  instances from reclamation while still charging nothing inside the Always Free limits.
  As of 2026-09-22 that exemption is **not** in Oracle's Free Tier FAQ or its Always Free
  documentation, so it is a report, not a fact. It also turns the card from identity
  verification into a card that can be billed.

**Supabase is what makes this survivable either way.** The data lives outside the host,
so a reclaimed or replaced machine costs a redeploy, not the price history. The host is
disposable; the data is not.

The decision is Emilio's and is not taken in this note. When it is, this section records
it, and the production compose file — with log rotation, since Docker's default log
driver never deletes anything — follows from it.

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

- Chosen the host. See above.
- Created the Supabase project, run `alembic upgrade head` against it, and put the two
  secrets where the host can read them.
- The production compose file and the deploy itself.
- The acceptance test: computer off, alert arrives.
- The README's final shape.
