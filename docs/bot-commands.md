# Bot commands

Phase 3: `/add`, `/list` and `/remove`, and the application lifecycle that phase 2
deliberately left out. This note explains the decisions, including three places where
the phase went beyond what was agreed and one thing a live run said that no test did.

Until now the two halves of this project — a scraper that reads a page and a schema that
keeps what it read — had never met. This phase is the join, and most of the decisions
below are about where the seam between Telegram and everything else should sit.

## A command returns text; only the adapter knows about Telegram

`bot/commands.py` holds three functions with this shape:

```python
async def add_tracking(resources: Resources, telegram_id: int, args: Sequence[str]) -> str
```

They import no `telegram`. `bot/handlers.py` holds the PTB adapters, and each is four
lines: find the user, call the command, send what came back.

The reason is testability, and it is not theoretical. Every test in this repo runs
offline, and the interesting assertions about `/add` are about rows and alerts — which
product row appeared, whether the alert was consumed, whether a request was made at all.
Building a `telegram.Update` to reach a `SELECT` would be a lot of ceremony bought with
nothing. `tests/test_bot_commands.py` exercises the real parser against a real captured
page, the real repository against a real database, and the real alert rule, with no
`Update` constructed anywhere.

It also draws the line in the right place for phase 4. The scheduled checker is a second
caller with no chat behind it, and everything in `commands.py` is already shaped for a
caller that cannot reply to anybody.

## The lifecycle lives in `post_init`, and this is not a style choice

`bot/runtime.py` opens one engine, one `httpx.AsyncClient` and one fetcher in PTB's
`post_init`, and closes them in `post_shutdown`.

The obvious place would have been `run()`, next to the settings. It is wrong, and wrong
in the way this project keeps running into: it works until it doesn't, somewhere else.
An async engine and an `AsyncClient` bind themselves to the event loop that is running
when they are constructed. `run_polling` starts that loop itself, *after* `run()` has
built everything — so anything built there belongs to a different loop, or to no loop,
and the bill arrives much later as a hang or a `got Future attached to a different loop`
raised from inside an innocent-looking handler. `post_init` runs after the loop exists
and before the first update is pulled, which is the only correct window.

There is a test that asserts `build_application` leaves `bot_data` empty, because "this
opens nothing" is the property that makes the rest true.

### One fetcher, not one per command

`HttpFetcher` holds the `robots.txt` cache and the time of the last request per host. A
fetcher built per `/add` remembers neither: the robots file is re-fetched every time and
the spacing between requests silently becomes zero. The conduct rules in the README
would still be written down and would have stopped being true — which is the failure
mode the rules were moved into code to avoid in the first place.

## `/add` cannot skip the fetch

The order is `fetch_product` → `upsert_product` → `set_tracking` → `record_success`, and
the first step is not optional.

A product is `(store, external_id)`, and Cyberpuerta does not put its `external_id`
anywhere in a URL — only in the page body (`docs/scraper-design.md`). So there is no way
to notice "we already track this" before the page has been read. Deduping on the URL
first is the natural optimisation, and it would key the whole system on the one field
the store demonstrably rewrites.

The fetch happens **outside** the session. Reading a store page takes seconds — a
robots.txt lookup, then a deliberate pause — and holding a transaction open across it
would make the slowest thing the bot does also the thing holding locks. Each command
opens exactly one `session_scope` afterwards, which is the unit of work.

## The error hierarchy finally earns its keep

Phase 1 built six error types instead of returning `None`, on the argument that each one
would become a different reply. This is that phase. The mapping is in `bot/copy.py`.

It is a `dict[type[ScraperError], str]` walked along the exception's MRO, not a chain of
`isinstance` tests. An ordered chain is correct exactly as long as nobody inserts a
subclass in the wrong place, and when it is wrong it is wrong **silently**:
`StoreRefusedError` listed after `FetchError` simply never matches, and every refusal
answers with a vaguer message forever. Nothing fails, nothing logs, and the only symptom
is a slightly unhelpful sentence.

The MRO walk also gives a future subclass a sane default — it inherits its parent's
message rather than falling through to "no sé por qué". That is imprecise but true,
which is the right thing for a default to be.

Python cannot check the mapping is exhaustive at import time, so a test does:

```python
@pytest.mark.parametrize("error_type", SHIPPED_ERRORS, ids=lambda t: t.__name__)
def test_every_scraper_error_has_a_reply_of_its_own(error_type)
```

`SHIPPED_ERRORS` filters `__subclasses__()` down to classes from `price_tracker.*`.
Without that filter the list would include throwaway error classes defined in other test
modules, and which ones exist depends on which files pytest has imported by then — a
test that passes or fails according to filenames is worse than no test.

`LayoutChangedError` is the only one logged with a stack trace. It is the only one that
means *this repo* is out of date; the rest are the store's weather.

## `/add` is a price reading, so it goes through `register_price`

This is the one decision that goes past the agreed plan, and it is deliberate.

`/add` reads a live page. That is an observation exactly like the one phase 4's checker
will make, so it is recorded in `price_history` and applied to the tracking through
`Tracking.register_price` — the same door phase 4 was told to use and never to work
around.

The consequence is the interesting part. If the price is already at or under the target,
`register_price` returns `True`, and the `/add` reply itself carries the good news:

```
Listo. Te aviso cuando baje.

Kingston SA400S37/240G SSD 2.5" SATA III
Precio ahora: $889.00 MXN
Tu objetivo: $1,500.00 MXN

🎉 Ya está en tu objetivo o por debajo.
```

Because the alert was consumed here, the checker will not announce the same price again
six hours later. The alternative — record the price but leave the alert armed — produces
a duplicate notification saying a price "dropped" when it did nothing of the sort.

The honest cost: if sending that reply fails, the alert has been spent on a message
nobody received. That is one lost notification for a price that has not changed, and the
next genuine drop still alerts, because a *lower* price re-qualifies.

Re-adding a product keeps both readings in `price_history`. Each one cost a request and
happened at a real moment; throwing one away to keep the table tidy would be discarding
measured data.

## Money in and money out

A target typed by a user goes through `money.to_cents()`, the same door the JSON-LD
price uses. `"1,899.00"` from a chat and `"1899.00"` from a store must become the same
integer, and a price that cannot be read exactly is refused rather than guessed at — so
`/add <url> gratis` is a message, not a row.

The way back out is new: `money.format_cents()`, integer arithmetic only. `cents / 100`
is float division, and it would have reintroduced floats in the one module that exists
to keep them away from money — on the last line, where nobody looks. A round-trip test
pins the two halves together.

`to_cents` is checked **before** the network. A typo should not cost the store a
request, and there is a test asserting the fetcher was never called.

## `/remove` takes a position, and what that costs

`/list` numbers what you track; `/remove 2` removes the second one. The alternative was
showing database ids, which makes the command exact and the list unreadable — and this
is a bot people type into on a phone.

The cost is real: a position means whatever the last `/list` said, and the list can move
under it. Two things keep it honest.

1. **The list is re-read inside the transaction that deletes.** The position resolves
   against the current state, never against a remembered one.
2. **The confirmation names the product.** If the wrong thing went, the user finds out
   in the same second rather than next week.

Removing a tracking leaves the product row and its history alone. Somebody else may be
watching it, and re-adding it later should not start from an empty chart.

## No parse mode, and the live run proved it

Every message is plain text. No HTML, no Markdown.

The reasoning was that product names come from a store and contain whatever the store
felt like, and the failure mode of HTML parse mode is not an ugly font — it is Telegram
rejecting the whole message with a 400, so the user gets nothing instead of getting it
unstyled. That was an argument until the live run answered with the real name:

```
Kingston SA400S37/240G SSD 2.5" SATA III
```

A double quote, from the very first product this bot was ever asked about. A URL on its
own line is still clickable without any markup, which is the only formatting these
messages need. Link previews are disabled, because a `/list` of five products would
otherwise render five cards.

## Three places this went past the plan

Flagged rather than buried, the same way phase 2 flagged `in_stock`.

- **`/help` exists**, and `/start` now prints it. `/start` is the only command most
  people will ever type, so it has to say what to type next. The phase 0 welcome — "por
  ahora solo sé saludar" — no longer describes the bot.
- **Long replies are split.** Telegram rejects a message over 4096 characters outright,
  with a 400, so a long `/list` would deliver *nothing* rather than most of itself.
  `split_message` prefers blank lines, falls back to line breaks, and only then cuts —
  so a chunk never opens with a price and no product name above it.
- **Every command is first contact.** `get_or_create_user` is called by `/list` and
  `/remove` too. There is no sign-up step in this bot, and a `/list` from a stranger is
  a perfectly good introduction that happens to have nothing to show.

## Verified live, once, end to end

The whole chain minus Telegram was run against the real store: real fetcher, real page,
real database file, real reply text. `/add` with a tracking parameter on the URL, then
`/list`, then `/add` again with a lower target, then a Liverpool link, then `/remove`.

The Liverpool link is worth calling out: at the time it was refused **without a request
being made**, because the store was not supported yet and the bot does not knock on the
door of a store it cannot read.

Phase 5 has since added Liverpool, and the second half of that sentence is the part that
aged well: the message naming the supported stores is derived from `PARSERS` rather than
written by hand, so it became "Cyberpuerta y Liverpool" on its own, in every message that
names them, with no edit to this file's code. The same link would now be fetched.

Sending `/add` from an actual phone is still owed, and it is the only part of this phase
a test cannot close. The `TypeHandler` in group -1 stays at INFO for exactly that reason:
phase 2's note suggested demoting it once real commands logged for themselves, but the
case it exists for is the one where *no* command runs, and a message matching no handler
leaves no other trace.

## What phase 3 deliberately does not do

- **Nothing is scheduled.** Prices are read when a user types `/add` and at no other
  time. The `JobQueue` extra is installed and unused; phase 4 is the checker, and it is
  a loop over `products_due_for_check`, `record_success` and `record_failure` that
  should not contain a single `select()` of its own.
- **Nothing retries.** A store that times out produces a message and no row. Retry
  policy belongs to the checker, which is the layer that knows how long it may wait.
- **No charts.** `Repository.price_history` is written and unused so far.
- **`/add` takes one product at a time**, and two simultaneous `/add`s of the same new
  product would race on the unique constraint. PTB processes updates sequentially in a
  single polling process, so today it cannot happen. If that ever changes, the fix is to
  catch `IntegrityError` and re-select, not to loosen the constraint.
