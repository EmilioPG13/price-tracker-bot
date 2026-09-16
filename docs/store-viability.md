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
uv run python scripts/store_spike.py --label home --out spike-results/home.json

# Datacenter network (free, unlimited on a public repo)
gh workflow run store-spike.yml
```

If every store is blocked from Actions, take a second free datacenter reading from
Google Colab before concluding: Azure ranges are more heavily blocklisted than an
average VPS, so Actions is the pessimistic measurement, not the representative one.

## Results

> Not yet measured. Fill in from the two runs above.

### From home (residential IP)

<!-- paste the spike's markdown table here -->

### From GitHub Actions (datacenter IP)

<!-- paste the spike's markdown table here -->

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
