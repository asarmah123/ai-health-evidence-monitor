# AI in Health — Clinical & Market Access Evidence Monitor

**Live → https://asarmah123.github.io/ai-health-evidence-monitor/**

Daily intelligence on how AI technologies move through healthcare — from research and clinical validation to regulation, health technology assessment, reimbursement and adoption.

`Curated public sources` · `regulators, HTA & payer bodies across 20+ markets` · `North America · Europe · APAC · LATAM · MEA`

It pulls public signals from regulators, journals, trial registries and industry press into one briefing that refreshes every morning — framed the way a market-access team thinks, around two questions: **can it be sold?** (authorisation) and **will it be paid for?** (coverage).

Built for **HEOR and market-access professionals** who need regulatory, evidentiary and payment signals in one place rather than five.

---

## Design principles

- **No language model at runtime.** Classification, ranking and dating are deterministic and reproducible — the same inputs produce the same output.
- **Primary sources first** — official regulator, HTA and registry APIs and feeds wherever available, supplemented by selected industry publications where no primary feed exists.
- **No causal or predictive claims** — it reports what changed and how unusual it is versus a recent baseline, never why or what's next.

---

## Features

- **Follow AI from research through reimbursement** — the six-stage evidence journey in a single view.
- **See what matters first** — the day's featured development plus the top-ranked updates, pulled to the top by explicit rule.
- **Spot what's unusual** — stage activity and term mentions compared against their own recent baseline (attention, not importance).
- **Track the two gates** — *can it be sold?* (authorisation) vs *will it be paid?* (coverage).
- **Read leading indicators** — trials registering an economic endpoint, peer-reviewed value papers.
- **Compare activity across regions and countries** — regulators, HTA and payer bodies across 15+ markets.
- **Explore and filter** — search and filter the feed by stage, region, source type and date.
- **Follow topics** — subscribe to curated evidence streams (FDA authorisations, CMS coverage, NICE evaluations, oncology AI, digital therapeutics…) via a shareable link and a per-topic RSS feed.
- **Subscribe via RSS** — the top-ranked items as a standard feed, plus a feed per Follow topic.
- **Download the data** — each build exported as CSV and JSON.
- **Private by design** — a static site with no server, no tracking and no cookies.

---

## How it's organised

- **Home** — daily briefing: key metrics, featured development, evidence journey and ranked updates.
- **Evidence** — searchable feed, filtered by stage, region, source type and date.
- **Analysis** — activity breakdowns, trends, commercial-pathway signals and baseline comparisons.
- **Methodology** — pipeline, sources, limitations, privacy and FAQ.
- **About** — purpose, audience and credibility principles.

An **[RSS feed](https://asarmah123.github.io/ai-health-evidence-monitor/feed.xml)** of the top-ranked items is published alongside the site, together with a feed per Follow topic and a CSV/JSON export of each build.

---

## Architecture

```
  Public sources (heterogeneous)
  ┌──────────────────────────────────────────────────────────┐
  │  arXiv · PubMed · openFDA · ClinicalTrials.gov ·          │
  │  Federal Register · EMA · NICE · ISPOR · HITAP · RAPS …   │
  └──────────────────────────────┬───────────────────────────┘
                                 │
                    GitHub Actions — daily ETL
                                 │
        ┌────────────────────────┴────────────────────────┐
        │  fetch → normalise → classify (stage / region /  │
        │  body) → deduplicate → rank                      │
        └────────────────────────┬────────────────────────┘
                                 │
                    Static HTML dashboard (GitHub Pages)
```

Everything runs on a schedule and renders to a single static page, so there is nothing to host and nothing to break at request time. Ingestion-path detail (REST APIs, RSS/Atom, E-utilities, scraping) is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Data sources

*Representative examples by layer — not the full list. The complete set of sources, exact queries and ranking configuration are maintained privately. A fuller inventory is in **[SOURCES.md](SOURCES.md)**.*

| Layer | Examples |
|---|---|
| **Research** | arXiv (cs.AI / cs.LG / cs.CL) |
| **Clinical evidence** | ClinicalTrials.gov, NEJM AI, Lancet Digital Health, Nature Medicine |
| **HEOR & HTA** | ICER, HTAi, ISPOR, PubMed AI × HTA searches |
| **Regulation** | FDA, EMA, MHRA, PMDA, ANVISA (openFDA, US Federal Register) |
| **Reimbursement** | CMS, NICE, DiGA, G-BA/IQWiG, CADTH, PBAC, CONITEC |
| **Industry** | STAT, Endpoints, Fierce, MedTech Dive |

---

## Methodology

- **Deduplication** by exact URL, then near-duplicate collapsing — the same event reported by several outlets is reduced to one, deterministically by shared distinctive tokens.
- **Classification** into stage, jurisdiction and body is rule-based and auditable.
- **Ranking** follows explicit, additive rules (device authorisations, economic-endpoint trials, major-regulator actions, recency); it reflects priority, not confidence, and every item exposes its own "why ranked" breakdown.
- **Dates are never estimated** — read from the source, or recorded as `unknown` and excluded from any date-based figure.
- **No causal or predictive claims** are made beyond what the counts support; company press releases are excluded to keep the feed independent.

Full definitions are in **[TAXONOMY.md](TAXONOMY.md)**. A separate clearance-to-coverage dataset is under development — see **[docs/DATASETS.md](docs/DATASETS.md)**.

---

## Stack

- Python ingestion pipeline
- GitHub Actions scheduled builds
- Static HTML / CSS / JavaScript frontend
- RSS feed generation (main + per-topic) and CSV/JSON export
- GitHub Pages hosting

---

## Licence & colophon

MIT licensed (see [LICENSE](LICENSE)). Developed with AI-assisted software engineering; the deployed monitor uses no language model at runtime — all classification, ranking and dating are rule-based and reproducible. Static site, rebuilt daily by GitHub Actions — no tracking, analytics or cookies; read state is stored in the browser only. Contributions and corrections welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)). Maintained by [@asarmah123](https://github.com/asarmah123).
