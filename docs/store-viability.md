# Store viability

Which stores can actually be read, from which networks, and what that forces about
where this bot runs.

This file is evidence, not opinion. Every row comes from `scripts/store_spike.py`.
Re-run the spike before changing a conclusion here.

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

### From GitHub Actions (datacenter IP)

> Pending: triggered by the commit that added these targets.

## Decision

> Pending measurement.

| Store | Home | Actions | Verdict |
|---|---|---|---|
| | | | |

**Store #1 (phase 1):** TBD
**Store #2 (phase 5):** TBD
**Where the bot runs (phase 6):** TBD

Decision rule agreed up front, so the data is not argued with after the fact:

| Observed | Conclusion |
|---|---|
| Answers from Actions | Strong candidate. Price checks can run on a free cloud cron. |
| Answers at home, `403` from Actions | Only viable on a residential IP. If no store clears Actions, the free deployment is a machine at home. |
| `403` from both | Out of the MVP. Documented in the README as an honest limitation. |
| `robots.txt` disallows the path | Out, regardless of whether a request would have succeeded. |

## Rules that do not bend

No rotating proxies, no captcha solving, no browser impersonation. The
`User-Agent` names the project and links to it, so any store operator can identify
and block this bot deliberately.

A store that blocks us gets dropped and written down here. "Measured it, it refused,
documented it" is the correct engineering outcome — and a far better answer in an
interview than a story about evading detection.
