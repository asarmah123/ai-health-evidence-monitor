# AI in Health — Clinical and Market Access Evidence Monitor

https://asarmah123.github.io/ai-heor-feed/

A daily feed covering artificial intelligence across evidence generation, device
authorisation, and reimbursement.

Sources include arXiv, PubMed, openFDA, ClinicalTrials.gov, the Federal Register, EMA,
NICE and ISPOR, alongside trade and research press. Items are tiered Daily / Weekly /
Monthly and filterable across six pipeline stages: AI research & models, clinical evidence
& trials, health economics & HTA, regulatory & authorisation, reimbursement & coverage, and
industry & funding.

## Clearance → coverage

The site tracks the time between market authorisation of an AI-enabled device and the
point at which reimbursement becomes obtainable, across the United States, Germany,
France and the United Kingdom. Aggregate figures are shown on the site: medians by
market, counts by status, and the shortest observed interval.

The dataset is being built up over time, so the figures are currently based on a small
number of devices.

Definitions matter here, because "covered" describes several different things — a CPT
Category III code, a provisional DiGA listing, a time-limited NTAP, and a regional LCD
are not equivalent. [TAXONOMY.md](TAXONOMY.md) sets out the definitions used, including:

- Refusals, withdrawals and delistings are recorded, with dates.
- Dates are not estimated. Where a date is unavailable it is recorded as `unknown` and
  excluded from medians.

Corrections and suggested changes to the definitions are welcome — please open an issue.

## Layout

- **Overview** — counts of recent regulatory actions, device clearances, trials with an
  economic or utilisation primary endpoint, and AI × HTA papers, plus which tracked terms
  are rising or falling.
- **Coverage** — median days from authorisation to reimbursement, by market.
- **The feed** — the underlying items, tiered and filterable.

## Colophon

Static site, rebuilt daily by GitHub Actions. No tracking, analytics or cookies. Read
state is stored in the browser only.

Maintained by [@asarmah123](https://github.com/asarmah123).
