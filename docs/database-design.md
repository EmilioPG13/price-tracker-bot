# Database design

Phase 2: persisting what the scraper reads. This note explains the decisions, including
one bug that the test suite could not have found and one place where the agreed data
model was widened.

Everything here runs on SQLite locally and on Postgres in production, and since phase 6
the test suite runs on both (`docs/deployment.md`). Most of this note is about the places those two disagree, because that is
where the interesting failures live: a bug that only exists on one backend shows up as
tests passing and production misbehaving, or the reverse.

## A product is `(store, external_id)`, and this is now enforced

Phase 1 established the rule and `docs/scraper-design.md` has the evidence: the same
Kingston SSD answers on the category URL, on the flat URL it 301s to, and on a third URL
it declares canonical. A URL-keyed table would have held three rows for one SSD.

The schema states it as a unique constraint, and there is a parametrised test that adds
the product through all three spellings — including the tracking-parameter form a user
would actually paste out of Telegram — and asserts one row comes back:

```python
@pytest.mark.parametrize("pasted", [CATEGORY_URL, SHARED_URL, OXID_URL])
async def test_every_spelling_of_one_url_reaches_one_row(...)
```

It runs against the real captured page, so it exercises `fetch_product` and
`upsert_product` end to end rather than asserting the constraint exists.

`canonical_url` and `name` are overwritten on every sighting. A store renaming a slug is
not a new product — it is the same product with a different link to show the user.

## Money stays integer cents, one layer further down

`price_tracker.money.to_cents()` refuses a price it cannot read exactly. Storing the
result through a `Numeric` column would have undone that quietly: on SQLite, SQLAlchemy's
`Numeric` round-trips through `float`, so 1899.00 can come back as 1898.9999999999998
with nothing raised anywhere along the way.

Every money column is `Integer`. There is a test that walks the metadata and asserts it,
because this is the kind of thing a future migration changes by accident:

```python
@pytest.mark.parametrize(("table", "column"), MONEY_COLUMNS)
def test_money_is_stored_as_integer_cents(table, column)
```

## Timestamps go through one type, and naive ones are refused

SQLite has no timezone type. `DateTime(timezone=True)` is accepted and then ignored, so a
timezone-aware datetime written on SQLite comes back naive; Postgres returns it aware.
The first comparison against `datetime.now(UTC)` then raises `TypeError: can't compare
offset-naive and offset-aware datetimes` — on one backend only.

`UtcDateTime` is a `TypeDecorator` that converts to UTC on the way in, **rejects a naive
datetime rather than guessing its zone**, and re-attaches UTC on the way out. That last
step is not a guess, because nothing naive was ever stored.

This is deliberately the same shape as `to_cents()`: one door in, and it fails loudly on
input it cannot convert exactly. The alert cooldown is measured in hours, and guessing a
zone is how twelve of them silently become six.

## Foreign keys are declared *and* switched on

Every `ON DELETE CASCADE` in the schema is a no-op on SQLite unless each connection runs
`PRAGMA foreign_keys=ON`. The default has been off since SQLite 3.6, for backwards
compatibility. Deleting a user leaves their trackings behind, pointing at a row that no
longer exists, and nothing raises.

`create_engine` attaches a connect listener that turns it on. The relationships use
`passive_deletes=True`, so the ORM does not load children in order to delete them one by
one — which means the database's cascade is the thing actually being exercised, and
`test_deleting_a_user_deletes_their_trackings` genuinely fails if the pragma is removed.

## The alert rule lives on the row, not in the checker

The rule agreed in phase 0:

```
alert if:  price <= target
       and (last_alerted_price IS NULL or price < last_alerted_price)
       and (last_alerted_at IS NULL or >= 12h elapsed)

on alert:        last_alerted_price = price
if price > target: last_alerted_price = NULL   # re-arms for the next drop
```

It is a function of one row's own state, so it is a method on `Tracking` rather than
something phase 4's checker will implement. `should_alert()` is the pure predicate;
`register_price()` applies a reading and returns whether to alert.

`register_price` exists as a single call for one reason: **the reset on the way up is the
half people forget.** Without it, a product that dropped to $889, recovered to $1,200 and
fell back to $950 stays silent forever, because $950 is not below the $889 already
announced — even though from the user's side it just crossed their target again. Pairing
a predicate with a separate setter makes omitting the reset easy; one call makes it
impossible.

Lowering a target re-arms too. A user who moves their target down is asking to be told
again, and leaving the alert state alone would silence exactly the drop they asked for.

## A bug the ORM tests could not have found

The `status` column is a `VARCHAR` with a `CHECK` constraint rather than a native
Postgres `ENUM`, because adding a value to a native enum needs its own migration and, on
older Postgres, cannot run inside a transaction. A `CHECK` behaves identically on SQLite,
which is what makes the test backend meaningful.

Writing it that way surfaced something worth recording. **SQLAlchemy persists a Python
enum by its member *name*, not its value.** So `ProductStatus.ACTIVE` was stored as
`ACTIVE`, the generated `CHECK` was over `('ACTIVE', 'RETIRED')`, and the `server_default`
— written as `ProductStatus.ACTIVE.value` — was `active`, which violates it:

```
sqlite3.IntegrityError: CHECK constraint failed: ck_products_productstatus
```

Every test passed regardless, because the ORM always sends the status explicitly and
never falls back to the server default. The only things that hit it are a raw `INSERT`
and a data migration — that is, a shell session or a deploy.

It was found by reading the DDL that `alembic upgrade head` actually produced, not by
running the suite. `values_callable` now makes the column store lowercase values, so the
column, the default and the constraint all say the same thing, and there is a regression
test that inserts a row through raw SQL and lets the default apply.

The general lesson is the one this repo keeps relearning: read what the thing produced,
not what it was asked to produce. It is the same shape as the store spike finding that a
`200` can be a lie.

## Alembic, and the three changes to the generated template

Initialised with `alembic init -t async`. The default template is synchronous and cannot
drive an async engine, which is the sort of thing that fails at the first `upgrade` on a
deploy rather than locally.

Three changes to the generated `env.py`, each of which is load-bearing:

1. **The URL comes from `DATABASE_URL`, not from `alembic.ini`.** The ini value is left
   blank deliberately — a connection string in a tracked file is how a production
   password ends up in a public repository. `DatabaseSettings` exists so a migration can
   read that URL without instantiating the full `Settings`, which would fail on a missing
   `BOT_TOKEN` that has nothing to do with a migration.
2. **`render_as_batch=True`.** SQLite cannot `ALTER TABLE` to drop a column, rename one,
   or add a constraint; batch mode emits the copy-into-a-new-table dance instead. It is
   harmless on Postgres and it is the difference between a migration that runs locally
   and one that only runs in production.
3. **`compare_type` and `compare_server_default`.** Without them, a change to a column's
   type or default is not detected and `--autogenerate` produces an empty migration that
   looks like success.

`script.py.mako` also carries `import price_tracker.db.models`, because autogenerate
renders a custom column type by its full path — without the import, any migration
touching a timestamp is a `NameError` waiting for the deploy.

## The test that stops the models and the migrations drifting apart

This is the Alembic failure everybody has: a model changes, the migration does not, and
nothing notices — because the tests build their database with `create_all` straight from
the models and never run a migration at all.

`tests/test_migrations.py` runs `alembic upgrade head` against an empty file and then
asks Alembic's own `compare_metadata` whether the result still differs from the models.
A second test downgrades to base, because the generated `downgrade()` is the half nobody
reads and the moment it is needed is the worst moment to find out it was wrong.

The drift test was checked by making it fail: adding a column to a model and confirming
it reported `Detected added column 'price_history.drift_canary'`. A guard nobody has seen
fail is not yet a guard.

## Where the data model was widened, and why

The agreed columns for `price_history` were `id, product_id, price_cents, checked_at`.
There is now also **`in_stock`**.

Phase 1 settled that an out-of-stock product is data rather than an error: it keeps its
price and flips availability, and it is arguably the most interesting thing to track,
since it may return cheaper. Recording only the price would draw a flat line through a
period when the thing could not be bought at all, which is the chart phase 4 renders.

This is a deviation from the agreed model and is flagged rather than buried. It is one
column and it is cheap to drop; re-adding it later is a migration against live rows.

## The repository is the seam, and it is not optional

`Repository` takes a session and is the only thing that touches persistent state, for
the same reason `scrapers.fetch_product()` is the only way to read a page: there are two
runtimes — the Telegram handler and the scheduled checker — and they must not grow two
different ideas of what "record a price" means.

Phase 4's `check_all_prices()` should be a loop over `products_due_for_check`,
`record_success` and `record_failure`, containing no `select()` of its own. If it needs
one, that query belongs here instead.

The repository does **not** own the transaction. `session_scope` does, and the caller
picks the boundary: the `/add` handler wraps one command; the checker wraps one product
at a time, so a store failing halfway through a run does not discard the prices already
read from the others.

Two details in there that were decisions rather than defaults:

- **`products_due_for_check` orders with an explicit `nulls_first()`.** SQLite sorts
  NULLs first in an ascending order and Postgres sorts them last, so without this a
  brand-new product is visited last in production and first in the tests. Nothing in a
  log would show it.
- **`expire_on_commit=False` on the session factory.** The default expires every loaded
  attribute after a commit, and reading one back emits a lazy SELECT that an async
  session cannot run. The symptom is a `MissingGreenlet` on an innocent `product.name`
  immediately after saving it.

Relationships are declared `lazy="raise_on_sql"` for the same reason: an unloaded
attribute then raises a message naming the relationship, instead of the greenlet error,
which says nothing about what was missing.

## Retiring a product, and what it costs

`consecutive_failures` is reset by any success, not decremented — a product that answered
is healthy, however badly last week went. Six failures in a row retires it, which at the
default six-hour interval is a day and a half: long enough to outlast an outage, short
enough not to keep spending requests on a page that has genuinely gone.

A `PageGoneError` retires immediately. A 404 does not improve with waiting, and spending
five more requests proving that would be rude as well as pointless — the conduct rules in
`docs/store-viability.md` are the reason the number of requests is a design input at all.

Retiring keeps the row. The price history survives, the user's tracking survives, and
pasting the URL again revives the product — because it parsed, so whatever retired it is
over.

`last_checked_at` is updated on failure too. It is the checker's scheduling field, not a
record of success; leaving it alone would make a failing product eligible again on every
single run, which turns one broken page into a tight loop against a store.

## What phase 2 deliberately does not do

- **Nothing is wired into `main.py`.** There is no engine created at startup and no
  command that writes a row. Phase 3 adds the commands and the application lifecycle
  together, because an engine with no caller is a guess about how it will be used.
- **No Postgres driver.** Nothing in this layer is SQLite-specific by design, which is
  what the choices above are for. `asyncpg` arrived in phase 6, and the whole suite then
  passed on Postgres without a change to a single model — see `docs/deployment.md`.
- **No retention policy on `price_history`.** It grows without bound. At one row per
  product per six hours that is ~1,460 rows a year per product, which is not a problem
  worth solving before there is a product count to solve it for.
- **No concurrency handling on `upsert_product`.** Two simultaneous `/add`s for the same
  new product would race on the unique constraint. The bot is a single polling process,
  so this cannot currently happen; if it ever runs more than once, the fix is to catch
  `IntegrityError` and re-select.
