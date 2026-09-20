# Fixtures

Real pages, saved byte-for-byte. `.gitattributes` marks this directory `-text` so git
never rewrites a line ending inside one — a capture that has been touched is not
evidence of anything.

They are large (250 KB to 1.1 MB each) and that is deliberate: trimming a page down to
the part the parser currently reads would mean the fixture only ever proves that the
parser still reads what it already read. The Liverpool pages make the point twice over —
most of what is in them belongs to *other* products, and that is exactly what the parser
has to not read.

| File | Captured | Source |
|---|---|---|
| `cyberpuerta-ssd-kingston-a400.html` | 2026-09-18 | Requested `https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html`, which 301'd to `https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html` |
| `cyberpuerta-ssd-acer-gm7.html` | 2026-09-18 | `https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/SSD-Acer-Predator-GM7-NVMe-1TB-M-2-PCI-Express-4-0-6300-MB-s-Escritura-7200-MB-s-Lectura.html` |
| `liverpool-licuadora-oster-2110245.html` | 2026-09-19 | `https://www.liverpool.com.mx/tienda/pdp/licuadora-oster-2110245-2-velocidades/1141535451` — answered directly, no redirect |
| `liverpool-batidora-oster-1110425673.html` | 2026-09-19 | `https://www.liverpool.com.mx/tienda/pdp/batidora-de-pedestal-10-velocidades/1110425673` |

## What each one is here to prove

**Kingston** is the page the store spike measured, and it carries the finding that
shaped the data model: one product, three URLs in a single page load — the one
requested, the one redirected to, and a third declared as canonical with a different
slug entirely. It is why `(store, external_id)` is the uniqueness rule and why
`external_id` is read from the page rather than from the URL.

**Acer GM7** is a second product from the same category, and exists so that the parser
is tested against the store's template rather than against one page. The test that the
two fixtures yield *different* `external_id`s is the one that would catch an id regex
which had accidentally latched onto something every page shares.

**Licuadora Oster** is Liverpool's page shape: a Next.js App Router app with its state
streamed inline through `self.__next_f.push(...)` and no structured data at all. It
carries the finding the parser is built around — **142 `productId` keys, 111 of them
distinct**, because the recommendation carousels ship a full priced record each. Only
the product the page is *about* sits under `productInfo`. A parser that took the first
price would have taken a neighbour's.

**Batidora Oster** is the one that caught a real bug before it shipped. It is discounted,
so its `salePrice` (7999) and its `promoPrice` (6399.2) differ and the page renders
`$6,399.20`. On the licuadora all three price fields are equal, which makes `salePrice`
look correct. Reading one page would have produced a parser that recorded the
pre-discount price on every product on sale. See `docs/liverpool-parser.md`.

Prices inside these files are frozen at capture time (889, 3,939, 1,078 and 6,399.20 MXN)
and will drift from the live store. That is fine — they are a parser contract, not a
price feed.

## Capturing another one

Politely, and only if a test needs a page shape the existing ones do not have: one
request, honest `User-Agent`, `robots.txt` checked first. `HttpFetcher` already does
all three, so the shortest correct way is a few lines using it.
