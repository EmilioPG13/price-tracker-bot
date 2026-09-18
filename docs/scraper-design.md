# Scraper design

Phase 1: reading a product's name and price from a Cyberpuerta page. This note explains
the decisions, including the two that were made against the obvious option.

Like `store-viability.md`, the claims here come from real pages. The two captures the
parser is tested against are in `tests/fixtures/`, with their provenance.

## Fetcher and Parser are separate classes

The alternative was one `Scraper` per store, which is shorter. The split exists because
the two things that break are unrelated and arrive on different days:

| What happens | What has to change |
|---|---|
| The store rewrites its HTML | a new `Parser`, same `Fetcher` |
| The store blocks our requests | a new `Fetcher` — an official API, say — same `Parser` |

There is a second benefit that matters more day to day: with the split, `Parser.parse`
is a synchronous function from a string to a `ProductData`, with no I/O in it. That is
why every parser test in this repo runs offline against a saved page, in milliseconds,
with no mocking.

This is not speculative generality, and there is a date on the proof. Liverpool was
chosen as store #2 *because* it ships no JSON-LD, so phase 5 has to write a genuinely
different parser against this same fetcher. A second JSON-LD store would have proved
nothing about the abstraction.

## `external_id` is the store's article id, not the manufacturer's SKU

This was the open question going into phase 1, and the answer was not the obvious one.

Cyberpuerta's JSON-LD offers a `sku`, and taking it would have been one line:

```json
"sku": "SA400S37/240G", "mpn": "SA400S37/240G", "gtin13": "0740617261219"
```

But `sku` is identical to `mpn` — it is the *manufacturer's* part number. It identifies
the hardware, not the listing. Two listings of the same Kingston part, a bundle, or a
refurbished unit would collide on it, and the `(store, external_id)` uniqueness rule
would then quietly merge two different products into one row.

The store runs OXID eShop, which gives every article a 32-hex internal id and publishes
it as the page's non-SEO address:

```
index.php?cl=details&anid=c156664ff9062de87fc3bf694dbb8eae
```

That is the primary key of the store's own article table: unique by construction, and
unchanged when a slug is rewritten. It is what `external_id` holds.

**The cost is real and worth stating.** `anid` sits in the page's serialised app state,
not in a declared contract like JSON-LD, so it is the likelier of the two to move. The
answer is not to key the database on the wrong thing to get an easier parse — it is to
notice when it disappears, which `LayoutChangedError` does, and to have a test that
would catch a regex which latched onto something every page shares (two fixtures, and
an assertion that their ids differ).

## One product, three URLs

The clinching evidence arrived in a single page load. Requesting the URL that the store
spike had measured:

```
requested   /Computo-Hardware/Discos-Duros-SSD-NAS/SSD/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html
301 to      /SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html
canonical   /Computo-Hardware/Discos-Duros-SSD-NAS/SSD/SSD-Kingston-A400-240GB-2-5-SATA-III-
            350-MB-s-Escritura-500-MB-s-Lectura.html
```

Three spellings of one SSD, and the store disagrees with two of them. Add the tracking
parameters that arrive when a link is shared from a phone and the count keeps going. A
URL-keyed products table would have held several rows for one product, each with its
own price history, and the duplicates would have looked like real data.

So: `canonical_url` is taken from the store's own `offers.url` rather than from the URL
we requested, and it is refreshed on every check, because it is only there to give the
user something to click. Identity lives in `external_id`.

`normalise_url()` therefore does less than its name suggests — it strips a pasted link
down to the page worth fetching, and cannot produce an `external_id`, because a
Cyberpuerta SEO URL does not contain one. Identity has to wait for the page. The one
exception it does handle is the `index.php?cl=details&anid=` form, where the query
string is not tracking but the whole address.

## Errors are types, not `None`

`None` would have collapsed four different situations into one, at exactly the moment
the difference matters. Each of these becomes a different reply to the user in phase 3:

| Error | Means | Whose problem |
|---|---|---|
| `UnsupportedUrlError` | not a product page of a store we support | the user's link |
| `FetchTimeoutError` | the store did not answer in time | transient, try later |
| `StoreRefusedError` | 401 / 403 / 429 | the store's decision, recorded not worked around |
| `PageGoneError` | 404 / 410 | the product is gone; stop checking it |
| `ProductUnavailableError` | the page carries no offer at all | nothing to record |
| `LayoutChangedError` | the page arrived and we could not read it | **ours** |

The last row is the reason the hierarchy is worth having. `LayoutChangedError` is the
only one that means this repo is out of date, so it is the only one that should reach a
maintainer rather than an apology to a user.

One distinction is narrower than it looks. A product that is **out of stock but still
priced** is not an error — it is a `ProductData` with `in_stock=False`, and it is
arguably the most interesting thing to track, since it may return cheaper.
`ProductUnavailableError` is only for a page with no price anywhere, where storing a row
would mean inventing one.

## Money never touches `float`

Prices are integer cents everywhere, and `money.to_cents()` is the only door in. It
takes what JSON-LD actually ships — `"1899.00"`, `889` unquoted, `499.5` as a JSON
number — and routes all of it through `Decimal`, using `str()` on floats so the digits
the store wrote are used rather than their binary approximation.

It refuses what it cannot read *exactly*, rather than guessing:

- `"1.899,00"` (European format) would be a silent 100× error if read as `1.899`
- a price with three decimals means the format moved and we are misreading it
- a missing `priceCurrency` is a failure, not a default to MXN — defaulting is right
  today and silently wrong the day this store quotes in dollars

The danger these guard against is not arithmetic drift. It is that a price read as a
float travels into SQLite and comes back as `1898.9999999999998`, with no error raised
anywhere along the way.

## Graduating from the spike, and leaving a copy behind

`extract_jsonld_product()` came out of `scripts/store_spike.py`, with its tests. The
spike keeps its own copy on purpose.

That is duplication, and it is deliberate: the spike is a throwaway measurement tool
whose findings are already filed in `store-viability.md`. If it imported from
`src/`, then every later refactor would silently rewrite the instrument that produced
evidence already recorded. The application's copy is free to grow fields the spike never
needed — `sku`, `availability`, the offer URL — without touching a measurement that has
already been made.

## Conduct is enforced in code, not described in prose

The README's "Scraping conduct" section is a promise. `HttpFetcher` is where it is kept:

- the `User-Agent` names the project and links to it, and there is no way to pass a
  browser string through the class,
- `robots.txt` is fetched once per host, cached, and a disallowed path is never
  requested — there is a test asserting the product request was not made,
- requests to one host are spaced out, honouring a longer `Crawl-delay` if the store
  asks for one,
- nothing is retried here at all.

That last one is a deliberate omission rather than a gap. A 403 asked twice is just
hammering, and the retry-on-5xx rule belongs to the scheduled checker in phase 4, which
is the layer that knows how long it is allowed to wait.

## Fixtures are real pages, kept whole

`tests/fixtures/` holds two pages Cyberpuerta actually served, byte for byte, at ~250 KB
each. Both parts of that are intentional.

Hand-written markup tests a parser against its author's assumptions, which is exactly
how a parser passes its whole suite and then fails on the store. And trimming a capture
down to the part the parser currently reads would leave a fixture that can only ever
prove the parser still reads what it already read.

The failure cases — no offer, no name, an unreadable price, a missing article id — are
synthetic, because capturing a real out-of-stock page means waiting for the store to run
out of something.

## What phase 1 deliberately does not do

- **No retry policy.** Phase 4, in the checker.
- **No Liverpool parser.** Phase 5, and it is supposed to be harder: no JSON-LD.
- **No database.** Phase 2. `ProductData` is a reading, not a row; it carries
  `store`, `external_id` and `canonical_url` because those are what the products table
  will be keyed and displayed by, and the parser is what learns them.
- **No user-facing strings.** The errors carry developer-facing English. Mapping them to
  Spanish replies is phase 3, and it is a presentation concern.
