# The price checker and the alerts

Phase 4: the scheduled run that reads prices nobody asked for, the messages it sends
when one crosses a target, and `/chart`. This is where the bot stops being something you
poke and starts being something that tells you things.

Until now a price was read only when a user typed `/add`. Everything below is about the
second runtime — what it is allowed to retry, what it writes before it speaks, and what
happens when one product in a run goes wrong.

## The checker is a job in the bot's own process

The alternative was a separate scheduled command run by cron. The argument for it was
that a scheduled run survives a host that puts idle processes to sleep, which matters
under the zero-cost constraint.

That argument turns out to be weaker than it looks: **this bot polls Telegram
continuously, so its process is never idle.** A host that sleeps it has already broken
the bot entirely, not merely the checker. What was left was an ordinary trade, and
in-process won it on three counts:

- **One writer.** Two processes writing the same SQLite file makes the
  `IntegrityError`-and-re-select case real, and every write path would need to handle a
  row appearing underneath it. In-process, that case stays hypothetical.
- **The bot is already there.** An alert needs a `Bot` to send it. A second process
  would have to build one from the token, so the token would be read in two places.
- **One thing to deploy.** Phase 6 has to put exactly one artefact somewhere.

The decision is reversible on purpose. `checker.check_all_prices()` takes a `Resources`
and a notifier callable and knows nothing about who called it, so moving to a separate
process means writing an entry point that builds both — not rewriting the checker. If
phase 6 lands somewhere that wants webhooks on a serverless host, that is the change.

## Two adapters, one service layer

The structure mirrors phase 3 exactly, and the symmetry is the argument:

| does the work, imports no `telegram` | the PTB adapter |
|---|---|
| `bot/commands.py` | `bot/handlers.py` |
| `bot/checker.py` | `bot/jobs.py` |

Both halves of the left column go through `scrapers.fetch_product()` and `Repository`,
which is the whole reason those exist. The two runtimes cannot drift into two different
ideas of what recording a price means, because there is only one piece of code that does
it.

`checker.py` takes a `Notifier` — `async (telegram_id, text) -> None` — instead of
returning strings the way a command does. That is the one real difference between the
runtimes: **a command has a message to reply to, and an alert has only a user id.** This
is the first time the bot talks *to* Telegram rather than being talked to. In a private
chat the user id is also the chat id, which is why nothing extra had to be stored.

In the tests the notifier is a list. That is the entire Telegram mock.

## Three transactions per product, not one

A run does this per product:

1. read the due list, close the session
2. fetch the page, with nothing held open
3. open a second session, record the result, work out who to tell
4. close it, then send

The rule is that **no transaction is ever open across a network call.** Reading a store
page takes seconds — a robots.txt lookup, a deliberate pause, then the request — and a
transaction held across it is the slowest thing the bot does holding locks while it
waits. `/add` already worked this way for one product; a run walks many in a row, so the
same mistake would be that much longer.

The cost is that a `Product` row cannot be carried across the gap. It is re-loaded by id
through `Repository.get_product`, which is new in this phase, and it may legitimately
have gone by then — `test_a_product_deleted_mid_run_is_dropped_not_crashed` is that case.

Each product also gets its **own** `session_scope`, so a store failing on the fourth
product does not discard the three prices already read. A run that threw everything away
on the last failure would lose work in proportion to how long it ran.

## Retry belongs here, and only for a 5xx

`HttpFetcher` retries nothing, deliberately: it has no idea how long its caller can wait.
The checker runs every six hours and can afford thirty seconds, so the policy lives here.

The narrowness is the conduct rule from the README, not caution:

- **5xx — retried, twice, at 5s and 30s.** The store is telling us it is broken. Waiting
  is the cooperative response, and it is usually a deploy.
- **403/401/429 — never retried.** The store is telling us to go away. Asking again is
  hammering, and per `docs/store-viability.md` a refusal is a finding to record rather
  than an obstacle to work around. `test_a_refusal_is_never_retried` asserts the request
  count, which is the promise.
- **A timeout — not retried either.** It is the one failure that might mean *we* are the
  problem. The next run in six hours is already the retry.

This needed a change one layer down. `HttpFetcher` collapsed everything at or above 400
that was not a refusal or a 404 into a generic `FetchError`, so a 500 was
indistinguishable from a DNS failure — the checker could not implement "retry a 5xx"
because nothing told it a 5xx had happened. `StoreUnavailableError` is now its own type.

That type is also a trap worth knowing about: it subclasses `FetchError`, so every test
asserting the parent passes either way. Someone could collapse it back and silently
disable the retry with nothing going red. `test_a_5xx_is_told_apart_from_every_other_failure`
uses `type(...) is` rather than `isinstance` for exactly that reason.

Adding the type also tripped the phase 3 guard, as predicted: `tests/test_bot_copy.py`
asserts every shipped `ScraperError` subclass has a Spanish reply of its own, and it
failed until this one got one. That is the guard working.

## Alerts are committed before they are sent, and that is a real trade

There is no ordering here that cannot lose something.

- **Send inside the transaction**, and a commit failure after a successful send means
  the bookkeeping is rolled back while the user has the message. They hear about the
  same drop again on the next run. It also holds a transaction open across a call to
  Telegram, which is the thing the previous section exists to avoid.
- **Commit first**, and a send failure means the bookkeeping says the user was told when
  they were not. They will not hear about this price again unless it falls further.

Committing first was chosen because the two failures are not equally bad. A missed alert
is one silent message. A rollback means the bot repeats itself, and the entire alert rule
— the cooldown, the beat-the-last-price condition, the re-arm — exists to stop this bot
being noisy. Losing an alert is a worse outcome per event and a better one per failure
mode. The failed delivery is logged, and `run.alerted` does not count it.

This is why `jobs.py` catches nothing. Swallowing a `Forbidden` from a user who blocked
the bot would report an alert nobody received; the one `except` in the system is in
`checker._send`, which is also the thing that does the counting.

A user who has blocked the bot therefore produces a logged failure every time a product
they track moves. Reaping them is later work, and is written down here rather than fixed.

## The alert rule is not re-implemented, and `/add` may have already used it

Every reading goes through `Tracking.register_price`, which is on the row. The checker
never touches `last_alerted_price_cents` or `last_alerted_at`.

The case that makes this matter is the phase 3 decision: `/add` reads a live page, so if
the price is already under target the `/add` reply **is** the alert, and it consumes it.
Had the checker implemented the rule separately, the user would be told the same thing
again a few hours later. `test_the_alert_add_already_sent_is_not_repeated` is that
scenario from this side of the seam.

The half people forget is the reset on the way up: a price going back above target
clears the armed price, so the next drop is a new event rather than a worse version of
the old one. It is inside `register_price` precisely so no caller has to remember it.

## The interval is the rate limiter, not the schedule

`products_due_for_check(interval=...)` decides what gets read; the job only decides when
to ask. A bot that restarts ten times an hour runs the check ten times and fetches
nothing, because nothing has aged past the interval. The first run is 60 seconds after
startup, which is courtesy rather than correctness — it keeps a crash loop from becoming
a request loop.

The same number is both the job's period and the due-query's threshold, and it is passed
through the job's `data` rather than read from settings in two places. They have to
agree; reading it twice is how they stop agreeing.

`check_batch_limit` caps one pass, because requests to one host are spaced out and a long
due list is minutes of deliberate waiting. Stopping early leaves the oldest handled and
the rest first in line next time, which is the order `products_due_for_check` already
returns.

## One product must not take the run down

The loop catches `Exception` around each product. That is broader than normal and it is
deliberate: **nobody is watching a scheduled job.** A crash halfway through would leave
every later product unchecked until the next pass, and being last in the list is not a
reason to go unchecked. It is counted separately as `run.crashed`, so a test asserting
zero is how an accidental bug stops being invisible; `handlers.on_error` does the same
job for commands.

`CheckRun` is returned rather than only logged, because a scheduled job that reports
nothing is a job nobody notices has stopped working.

## Charts

`/chart <número>` takes a position from `/list`, the same as `/remove`, and resolves it
against a list re-read inside the command rather than against whatever the user last saw.

Three decisions worth defending:

**Never `pyplot`.** It keeps a global registry and a notion of the current figure. Every
handler in this bot is a coroutine on one loop, so two `/chart` commands running at once
would draw into each other — and the failure is not a crash, it is the wrong picture sent
to the right person. `Figure` plus `FigureCanvasAgg` owns nothing global. Rendering then
happens in `asyncio.to_thread`, which is only safe *because* the figure is local.

**A step plot, not a line.** A straight segment between two readings claims the price
moved gradually between them, which is something we never observed: a price holds until
the next check and then jumps. `steps-post` draws what was measured. Out-of-stock
readings are marked rather than dropped, for the same reason they are recorded at all —
a chart that hid them would draw a confident line through a week when the thing could not
be bought.

**Two readings minimum.** One point is a dot in an empty box, and the moment a user is
most likely to try `/chart` is right after `/add`, when that is all there is. Saying so
is better than sending a picture that looks like the feature is broken.

### What the first live chart got wrong

The tests all passed and the chart was useless. Every reading of a freshly tracked
product falls on the same day, and the axis formatter was a fixed `%d %b` — so the
x-axis read `19 Sep` nine times across. Nothing raised. `ConciseDateFormatter` picks its
unit from the span it is given, so hours now show as hours and a three-month history
still shows as dates.

It is written down because of how it was found: not by a test, but by rendering one and
looking at it. `_build_figure` was split out of `_draw` afterwards so the axis labels
could be asserted, since "does it say hours or dates" is a fact even though most of what
a chart does is only judgeable by eye.

## Verified live, end to end

Run against the real Cyberpuerta, on a copy of the local database, with Telegram replaced
by a list — everything real except the delivery:

```
RUN: checked=1 ok=1 failed=0 retired=0 alerted=1 crashed=0

🎉 Está en el precio que pediste.

Kingston SA400S37/240G SSD 2.5" SATA III
Ahora: $879.00 MXN
Tu objetivo: $2,000.00 MXN
https://www.cyberpuerta.mx/...
```

Note the headline. The price had not moved — 879 both before and after — so the message
says it is *at* the target rather than claiming a drop that never happened. The alert
rule fires on any reading at or under target that beats the last one announced, and the
first such reading may have no previous price to compare against, so the sentence follows
the evidence. That branch had never run outside a test until this.

## What phase 4 deliberately does not do

- **No per-user check intervals.** One interval for the whole bot.
- **No reaping of users who blocked the bot.** Their alerts fail and are logged.
- **No alert digest.** Three products crossing at once send three messages. Batching them
  is the kind of thing worth doing once there is a user with three products.
- **No chart on the alert itself.** An alert is text; the picture is asked for. Rendering
  one per alert spends CPU on something most people will not look at.
- **No second store.** Liverpool is phase 5, and it is the one that has to prove a
  genuinely different `Parser` fits behind the same `Fetcher`.
