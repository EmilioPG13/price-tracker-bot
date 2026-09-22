# The Liverpool parser

Phase 5 added a second store. Liverpool was chosen in `store-viability.md` *because* it
is the harder one: it ships no JSON-LD, so it could not be read the way Cyberpuerta is.
That was the point. A second store that also shipped clean structured data would have
proved nothing about `Fetcher` and `Parser` being separate types.

This note is about what the page turned out to contain, the two decisions that were
wrong on first reading and were caught by capturing a second page, and the one
assumption this parser makes that Cyberpuerta's refuses to.

## The open question, and the answer

Phase 5 opened with a question that decided whether Liverpool was viable at all: is the
price in an embedded JSON payload, or only in rendered markup? The first is a data
structure and tolerable. The second is CSS selectors against generated class names, and
would have been grounds to demote the store.

The answer is the good one. Liverpool is a Next.js **App Router** application, and an
App Router page streams its server-rendered state inline:

```html
<script>self.__next_f.push([1,"3a:{\"productInfo\":{\"title\":\"Licuadora ..."])</script>
```

The captured page carries 113 of those calls. Concatenating the strings — 112 of them;
one `push([2,null])` carries no data — reassembles roughly 523 KB of payload out of a
1.09 MB page. That is what `scrapers/nextjs.py` does, and it is the only module in the
repo that knows anything about Next.js.

This is worth saying precisely, because "the price is in a JSON blob" is often said
about pages where it is not: the spike's own probe reported **no JSON-LD Product and no
price** for Liverpool, and that was correct. There is no `schema.org` anywhere on the
page, no `__NEXT_DATA__` (that is the *Pages* router, a different format), and no
`application/json` script tag. Searching for the obvious markers finds nothing. The data
is there under a name that only the App Router uses.

### What that contract is actually worth

Weaker than JSON-LD, considerably stronger than a class name.

JSON-LD is a published vocabulary a store commits to for search engines; that is why
`jsonld.py` is tried first wherever it exists. `__next_f` is a framework's private wire
format. Next.js has already renamed this once — `__NEXT_DATA__` to `__next_f` across the
Pages-to-App transition — and can do it again.

The alternative, though, was reading Liverpool's rendered markup, and Liverpool's markup
is Tailwind-style generated class names and `data-testid` attributes like
`1141535451-configurator-price`. Those are strings a build tool emits. They change when
the build changes, which is more often than the component tree does. The payload is the
data the components are rendered *from*: a field leaves it when the store stops using
the field, not when someone reruns the bundler.

When the format does move, `rsc_payload` returns `""`, the parser raises
`LayoutChangedError`, and that error means exactly what it is documented to mean — this
repo is out of date, not the user's link and not the store. That is the whole reason the
error hierarchy distinguishes it.

## The page contains 111 other products, all priced

This is the trap that would have shipped, and it is not subtle once seen. The licuadora
page carries **142 `productId` keys, 111 of them distinct**. The extras are the
recommendation carousels — "Artículos relacionados", "También te puede interesar" — and
every one of them ships a full record with a price.

The first `promoPrice` in the document does not belong to the product the page is about.
It belongs to a blender Liverpool felt like recommending that morning.

So the parser cannot look for a price. It has to look for *this product's* price, and
the payload gives a clean way to do it: the main product is the only record under a
`productInfo` key. The carousel entries carry `priceInfo` directly, in a different shape
(`minimumPromoPrice`, `maximumListPrice`) that the main record does not use. Both
captured pages contain exactly one `productInfo` and 140 `minimumPromoPrice`.

`nextjs.json_object(payload, "productInfo")` is therefore key-anchored rather than
positional, and `tests/test_liverpool_parser.py` asserts that a carousel price
(`2149`) is present in the fixture while the parsed price is the product's own.

## `salePrice` is not the price you pay

The most expensive thing phase 5 nearly got wrong, and the reason for capturing a second
page rather than one.

The first fixture, an undiscounted marketplace product, reads:

```json
"priceInfo": {"salePrice": 1078, "listPrice": {"price": 1078}, "promoPrice": {"price": 1078}}
```

Three fields, all equal. Any of them produces the right answer, `salePrice` has the most
promising name, and a parser written against that page alone passes every test.

The second fixture is a discounted first-party product:

```json
"priceInfo": {"salePrice": 7999, "listPrice": {"price": 7999}, "promoPrice": {"price": 6399.2}}
```

The page renders **$6,399.20**, with $7,999.00 struck through beside it. `salePrice`
here is the price *before* the discount. Reading it would have recorded a number wrong
by exactly the size of the discount — on precisely the products a price tracker exists
to catch, and in a way no amount of re-reading the first fixture would have revealed.

So the parser reads `promoPrice.price`, and deliberately **does not fall back** to
`salePrice` when it is missing. A fallback here does not degrade gracefully; it degrades
into silently storing the wrong price. Missing `promoPrice` raises `LayoutChangedError`
instead. This is the same argument as money never touching `float`: the failure that
matters is not the crash, it is the plausible-looking wrong number.

Both fixtures are in the suite, and the discounted one asserts `639920` and not `799900`.

Note also `6399.2` — a JSON float. `money.to_cents` routes non-strings through `str()`
precisely so the decimal digits are used rather than a binary approximation, which is
why the price lands as `639920` cents and not something ending in `19999`.

## `external_id` is the listing id, read from the page

Liverpool puts its product id in the URL — `/tienda/pdp/<slug>/1141535451` — which
Cyberpuerta does not. It would have been free to slice it off the path.

The parser reads `productInfo.productId` out of the payload instead, for three reasons.
The page is the authority on what it is about; a link can arrive after a redirect; and a
share link carries `?skuId=`, a *variant* id that is not always the listing id. Reading
identity from the page also keeps both parsers telling the same story, which matters
more than either one being marginally shorter.

Two smaller findings on the id itself:

- **It is not a fixed width.** The links inside the captured pages carry 10, 11 and 12
  digit ids (`1141535451`, `99991606363`, `999673847131`). A `\d{10}` in the URL pattern
  would have rejected real products and told the user Liverpool was unsupported.
- **The id is stable across slugs, and the slugs disagree.** The payload's own share
  link spells the licuadora `licuadora-2110245-2-velocidades`; the `<link rel="canonical">`
  says `licuadora-oster-2110245-2-velocidades`. One product, two addresses, in one page
  load — the same finding Cyberpuerta produced in phase 1, from a completely different
  store architecture. `canonical_url` comes from the canonical link, which is the
  store's own declared answer.

### `?skuId=` is dropped, and that has a cost

`normalise_url` drops the whole query string. Most of it is tracking, but `skuId` is
not: it selects a size or colour of the same listing.

Dropping it is deliberate. `external_id` is the product id that every variant shares, so
two variant links collapse to one row in `products` either way; keeping the parameter
would only make the stored `canonical_url` flap between whichever variant was pasted
last. The honest statement of the limitation: **this bot tracks a listing's price, not a
particular size's.** For the catalogue items it is aimed at that is the same number. For
apparel it may not be.

## The one assumption: currency

`cyberpuerta.py` refuses to assume a currency, and says why — assuming would be right
today and silently wrong the day the store quotes in USD. That rule is right where the
store declares one. Liverpool does not.

`currencyIsoCode` appears **four times in the marketplace fixture and zero times in the
first-party one**. It belongs to third-party seller offers. A product Liverpool sells
itself states no currency anywhere on the page.

So the choice was between an assumption and not supporting the store. The parser assumes
`MXN` — and makes it an assumption **with a tripwire** rather than a bare constant:
where the page *does* declare currencies, MXN must be among them, or the parse fails.

That deliberately does not fire on a mixed page, since a cross-border listing may
legitimately quote another currency alongside pesos. It does fire if Liverpool moves off
pesos wholesale, which is the scenario that would otherwise corrupt every stored price
without raising anything. It is a weaker guarantee than Cyberpuerta's and it is written
down here rather than hidden in a default argument.

## `inventoryStatus`, and why the type is checked

Stock is `productInfo.inventoryStatus`, a boolean. Out of stock remains *data* rather
than an error, exactly as in phase 1: a `ProductData` with `in_stock=False` and a price,
which is arguably the most interesting thing to track.

The parser rejects a non-boolean `inventoryStatus` instead of coercing it. This looks
fussy and is not: `bool("false")` is `True`, so a store that switched the field to a
string would not crash anything — it would report every sold-out product as buyable, for
as long as it took someone to notice. Same family of bug as a price read as `float`.

A **missing** `inventoryStatus` is taken as in stock, which mirrors the Cyberpuerta rule
for a missing `availability`: a priced product with nothing said about stock is more
likely for sale than not.

## The framework elides values, and they look like strings

Next.js writes an omitted value as the literal string `"$undefined"` and a reference to
another row of the stream as `"$5e7"`. Both appear in the captured payloads —
`crossSellProducts`, `discountLabel`, the warranty body.

They are placeholders, not data, and without handling they would end up in a product
name shown to a user. `liverpool._text` treats any string starting with `$` as absent.
The cost is that a product genuinely named `"$5 pesos"` would lose its name and the
parse would fail loudly — which is the right side to err on.

## What did not have to change

This is the part worth defending in an interview, because it is the return on phase 1's
architecture rather than anything phase 5 invented.

Adding a whole second store, with a completely different page architecture, touched:

- `scrapers/nextjs.py` and `scrapers/liverpool.py` — new,
- `scrapers/__init__.py` — one parser appended to `PARSERS`, and the exports.

And nothing else. Specifically **not**:

- `HttpFetcher`, which fetched a 1.09 MB Next.js page with the same honest `User-Agent`
  and the same robots check it uses for Cyberpuerta;
- `bot/commands.py`, `bot/checker.py` or any handler — they go through
  `scrapers.fetch_product`, which routes by URL and hands back a `ProductData`;
- `bot/copy.py` — `STORES` is derived from `PARSERS`, so every "solo leo links de …"
  sentence became "Cyberpuerta y Liverpool" on its own. The two-store branch of
  `_store_names`, written in phase 3 against a fake parser because there was only one
  real store, ran in production for the first time and was correct;
- the database. `(store, external_id)` already accommodated a second store's id format.
- `scrapers/errors.py` — no new error type was needed, so the guard in
  `tests/test_bot_copy.py` that demands a Spanish reply per error never fired. Phase 4
  tripped it; phase 5 did not need to.

Two existing tests did change, and both for the same honest reason: they used a Liverpool
URL as their example of an unsupported store. They now use Mercado Libre, which
`store-viability.md` records as deferred.

## `robots.txt`

Checked before writing a line of parser, because a disallow would have ended phase 5
regardless of how readable the page was.

Liverpool's `robots.txt` answers `200` and contains **no `Disallow` rule at all** — for
`*` or for any of the ~25 named agents it lists individually, several of which are AI
crawlers. Every path is allowed and no `Crawl-delay` is set, so `HttpFetcher`'s own
2-second floor applies.

A dead product URL returns a genuine **HTTP 404**, so `PageGoneError` already covers
retirement and the checker needs no Liverpool-specific handling.

## Verified live, 2026-09-19

Against the real store, through `fetch_product` — the same seam `/add` and the checker
use — and then through the command layer against a throwaway database.

Four pages, one fetcher, both stores in one run:

| Product | Read |
|---|---|
| `1141535451` licuadora (fixture) | `$1,078.00 MXN`, in stock |
| `1110425673` batidora, discounted (fixture) | `$6,399.20 MXN` — the promo price, not the $7,999 list |
| `1100215191` batidora manual — **not in the fixtures** | `$668.00 MXN` |
| Kingston A400 (Cyberpuerta) | `$879.00 MXN` |

The third one is the one that matters. It was never captured, and the parser was not
written against it. Its carousel entry in the *other* fixture renders `$668.00$959.00` —
a discounted product — and the live read returned `$668.00`. That is independent
confirmation of the `promoPrice` decision on a page the parser had never seen.

Through the command layer, with the link pasted carrying `?utm_source=telegram`:

```
Listo. Te aviso cuando baje.

OSTER Batidora manual 5 velocidades
Precio ahora: $668.00 MXN
Tu objetivo: $700.00 MXN
https://www.liverpool.com.mx/tienda/pdp/batidora-manual-5-velocidades/1100215191

🎉 Ya está en tu objetivo o por debajo.
```

`/list` then showed both stores together, and an unsupported link answered *"Por ahora
solo leo links de Cyberpuerta y Liverpool"* — the derived sentence, correct without
anyone editing it.

### From the phone, 2026-09-22

The one link the runs above could not reach was Telegram itself. Closed from a phone,
against the running bot and the same discounted product:

- `/add` answered `OSTER Batidora manual 5 velocidades`, `$668.00 MXN` — unchanged three
  days on — with the 🎉 line, since 668 was already under the 700 target. **The name
  rendered intact in the chat bubble**, which was the thing only this could show.
- **No link preview on the bot's reply.** The one in the screenshot sits on the *user's*
  outgoing message, where the Telegram client generates it; the bot's
  `LinkPreviewOptions` still hold for a second store.
- `/list` showed the Cyberpuerta and Liverpool trackings together, numbered.
- `/chart 2` answered that there is not enough history yet. Correct: a product added
  seconds earlier has exactly one reading, and two is the floor for a chart. **A Liverpool
  chart has therefore not been seen yet**; it needs the checker to take a second reading
  first.

### One cosmetic thing, on purpose

The payload splits the name: `brand: "OSTER"`, `title: "Licuadora 2110245 2
velocidades"`. The page's `h1` shows the title alone, which in a `/list` mixing stores is
not enough to tell products apart, so the parser puts the brand in front.

Liverpool writes brands in capitals, so names read `OSTER Batidora manual 5 velocidades`
rather than the store's own `Batidora manual Oster 5 velocidades`. That is the store's
string used verbatim. Title-casing it was considered and rejected: it would turn `LG`
into `Lg`, and inventing capitalisation is how a name stops being the store's.

## What phase 5 deliberately does not do

- **No variant-level tracking.** `?skuId=` is dropped; the tracked price is the
  listing's. See above.
- **No marketplace seller choice.** The licuadora has three sellers and an `offersCount`.
  The parser reads the price Liverpool puts on the page, which is the best offer's, and
  does not model the others. Tracking "the price of this listing" is the promise.
- **No CSS-selector fallback** for when the payload moves. A fallback that quietly
  produces a number from generated class names is worse than a `LayoutChangedError` that
  says the parser is out of date.
- **No captured out-of-stock page.** `inventoryStatus: False` is covered by a synthetic
  test; both captures are of products in stock, because capturing a real sold-out page
  means waiting for Liverpool to run out of something.
- **No Mercado Libre.** Still deferred, still for the reason in `store-viability.md`:
  the price is not in its server HTML at all, and its API needs a registered OAuth
  application.
