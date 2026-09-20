# Store viability

Which stores can actually be read, from which networks, and what that forces about
where this bot runs.

This file is evidence, not opinion. Every row comes from `scripts/store_spike.py`.
Re-run the spike before changing a conclusion here.

**What the numbers below are.** They are the record of the run that *decided* the store
choice, dated and frozen. `docs/spike-runs/` holds the live JSON, which the CI workflow
overwrites on every run, so its byte counts and prices drift away from this page by
design — a store changing a price is not this document going stale. Do not "fix" a
disagreement between the two; if a conclusion here needs revisiting, re-run the spike
and write a new dated section.

## Why this exists

A price tracker is worth nothing if the store refuses the request. Two facts make
that a real risk rather than a hypothetical:

- Mercado Libre's API returns `403` to anonymous traffic — it requires a registered
  application and an OAuth token.
- Its storefront blocks datacenter IP ranges, which is exactly what every free
  hosting tier hands out.

So "does it work?" has to be answered per store *and* per network, before six weeks
of work depend on the answer.

## How to reproduce

```bash
# Residential network (your machine)
uv run python scripts/store_spike.py --label home --out docs/spike-runs/home.json
```

The datacenter run happens in CI: editing `spike/targets.toml` triggers
`.github/workflows/store-spike.yml`, which probes the same targets from an Actions
runner and commits `docs/spike-runs/github-actions.json` back. Raw JSON for both runs
lives in `docs/spike-runs/`.

If every store is blocked from Actions, take a second free datacenter reading from
Google Colab before concluding: Azure ranges are more heavily blocklisted than an
average VPS, so Actions is the pessimistic measurement, not the representative one.

## Results

### From home (residential IP) — 2026-09-17

Egress: `138.186.31.124` (AS17072 Total Play Telecomunicaciones)

| Store | Verdict | HTTP | robots | JSON-LD Product | Price read | ms |
|---|---|---|---|---|---|---|
| mercadolibre | SERVED, no price | 200 | allows | no | — | 344 |
| amazon-mx | OK (price read) | 200 | allows | yes | 1295 MXN | 1516 |
| liverpool | SERVED, no price | 200 | allows | no | — | 1483 |
| cyberpuerta | OK (price read) | 200 | allows | yes | 879 MXN | 1064 |
| walmart-mx | OK (price read) | 200 | allows | yes | 1252.53 MXN | 860 |

Every store answered and every one allows the path in `robots.txt`. Three expose a
`schema.org/Product` with a price. Mercado Libre and Liverpool serve a `200` but no
JSON-LD Product, so they would need CSS selectors — a more brittle contract, and a
cost to weigh against how much either store is actually wanted.

### From GitHub Actions (datacenter IP) — 2026-09-18

Egress: `64.236.145.86` (AS8075 Microsoft Corporation)

**Every store returned `200`.** Taken alone that reads as "nothing blocks us", and it
is wrong. See the comparison below.

### Comparison — the measurement that actually decided

`uv run python scripts/store_spike.py --compare docs/spike-runs/home.json docs/spike-runs/github-actions.json`

| Store | Home bytes | Actions bytes | Ratio | JSON-LD | Reading |
|---|---|---|---|---|---|
| mercadolibre | 22,824 | 22,824 | 1.00x | no → no | Identical treatment, price not in server HTML |
| amazon-mx | 1,015,482 | 1,036,734 | 1.02x | **yes → no** | Same page, structured data withheld |
| liverpool | 1,083,470 | 1,083,469 | 1.00x | no → no | Identical treatment, price not in server HTML |
| cyberpuerta | 247,996 | 247,996 | 1.00x | yes → yes | Identical treatment, price readable |
| walmart-mx | 278,961 | **13,599** | **0.05x** | yes → no | Body collapsed — a challenge page wearing a `200` |

**The finding worth keeping: a status code lies.** Walmart does not answer a
datacenter IP with `403`; it answers `200` and 13 KB of anti-bot interstitial. A spike
that only compared status codes would have cleared all five stores and been wrong
about two of them. Body size across two networks is the cheap tell, which is why
`--compare` is now part of the tool rather than a one-off shell command.

## Decision

| Store | Verdict | Why |
|---|---|---|
| **cyberpuerta** | **Store #1** | Byte-identical from both networks, clean `schema.org/Product`, price read. No friction anywhere. |
| **liverpool** | **Store #2** | Not blocked — treated identically from both networks — but the price is not in JSON-LD. Needs a different parser. |
| amazon-mx | **Out** | Conditions of Use prohibit "data mining, robots, or similar data gathering and extraction tools" without written consent. Out on terms, independent of the measurement — and it withheld its structured data from CI anyway. |
| walmart-mx | **Out** | Blocked from datacenter IPs behind a fake `200`. Would only work on a residential IP. |
| mercadolibre | **Deferred** | Not blocked, but 22 KB either way: the price is not in the server HTML at all. Would need the official API, which requires a registered OAuth application. Revisit only if the MVP ships early. |

**Where the bot runs:** a free datacenter host is viable, because the two chosen
stores are indifferent to the network. The phase-6 fallback of running on a home
machine is not needed.

Liverpool being the second store is a better outcome than a second JSON-LD store
would have been. It forces a genuinely different `Parser` against the same `Fetcher`,
which is the real test of the phase-1 split — a second store that also shipped clean
JSON-LD would have proved nothing.

### Liverpool, resolved — 2026-09-19

The measurement above left one question open, and it was the one that could still have
demoted the store: **"no JSON-LD" does not say whether the price is in an embedded
payload or only in rendered markup.** The first is a data structure worth parsing; the
second is CSS selectors against generated class names, and would have been grounds to
drop Liverpool rather than ship something that broke on every redeploy.

Answered by capturing two real pages: the price **is** in an embedded payload. Liverpool
is a Next.js App Router app that streams its server state inline through
`self.__next_f.push(...)` — no `schema.org`, no `__NEXT_DATA__`, no `application/json`
tag, which is why the spike's probe correctly reported nothing. Roughly 523 KB of
payload inside a 1.09 MB page.

Liverpool is confirmed as store #2 and is live. Three further facts from the capture,
none of which changes the verdict:

- `robots.txt` answers `200` and contains **no `Disallow` rule at all**, for `*` or for
  any of the ~25 agents it names individually. No `Crawl-delay` either.
- A dead product URL returns a genuine **HTTP 404**, so `PageGoneError` already covers
  retirement without store-specific handling.
- The store never declares a currency for its own products. That forced the one
  assumption in the parser, and it is documented rather than hidden.

Full reasoning, including the two readings that were wrong on first pass, is in
`docs/liverpool-parser.md`.

### `robots.txt`

All five stores allow the probed path. No store was excluded on `robots.txt` grounds;
Amazon was excluded on its Conditions of Use, which is a separate permission that
`robots.txt` says nothing about.

## Rules that do not bend

No rotating proxies, no captcha solving, no browser impersonation. The
`User-Agent` names the project and links to it, so any store operator can identify
and block this bot deliberately.

A store that blocks us gets dropped and written down here. "Measured it, it refused,
documented it" is the correct engineering outcome — and a far better answer in an
interview than a story about evading detection.
