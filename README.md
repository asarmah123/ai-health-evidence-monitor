# AI in Health — Clinical & Market Access Evidence Monitor

**Live → https://asarmah123.github.io/ai-health-evidence-monitor/**

Daily intelligence on how AI technologies move through healthcare — from research and clinical validation to regulation, health technology assessment, reimbursement and adoption.

`~65 curated sources` · `regulators & HTA bodies across 15+ markets` · `North America · Europe · APAC · MEA`

It pulls public signals from regulators, journals, trial registries and industry press into one briefing that refreshes every morning — framed the way a market-access team thinks, around two questions: **can it be sold?** (authorisation) and **will it be paid for?** (coverage).

Built for **HEOR and market-access professionals** who need regulatory, evidentiary and payment signals in one place rather than five.

---

## Design principles

- **No language model at runtime.** Classification, ranking and dating are deterministic and reproducible — the same inputs produce the same output.
- **Primary sources first** — official regulator, HTA and registry APIs and feeds, not second-hand summaries.
- **Dates are never inferred** — read from the source, or shown as "date unknown" and excluded from date-based figures.
- **No causal or predictive claims** — it reports what changed and how unusual it is versus a recent baseline, never why or what's next; every ranking shows *why* it ranks where it does.

Rebuilt automatically every morning.

---

## Features

- **Follow AI from research through reimbursement** — the six-stage evidence journey in a single view.
- **See what matters first** — the day's featured development plus the top-ranked updates, pulled to the top by explicit rule.
- **Spot what's unusual** — stage activity and term mentions compared against their own recent baseline (attention, not importance).
- **Track the two gates** — *can it be sold?* (authorisation) vs *will it be paid?* (coverage).
- **Read leading indicators** — trials registering an economic endpoint, peer-reviewed value papers.
- **Compare activity across regions and countries** — regulators and HTA bodies across 15+ markets.
- **Explore and filter** — search and filter the feed by stage, region, source type and date.
- **Subscribe via RSS** — the top-ranked items as a standard feed.
- **Private by design** — a static site with no server, no tracking and no cookies.

---

## How it's organised

- **Home** — daily briefing: key metrics, featured development, evidence journey and ranked updates.
- **Evidence** — searchable feed, filtered by stage, region, source type and date.
- **Analysis** — activity breakdowns, trends, commercial-pathway signals and baseline comparisons.
- **Methodology** — pipeline, sources, limitations, privacy and FAQ.
- **About** — purpose, audience and credibility principles.

An **[RSS feed](https://asarmah123.github.io/ai-health-evidence-monitor/feed.xml)** of the top-ranked items is published alongside the site.

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
        │  body) → deduplicate (by URL) → rank             │
        └────────────────────────┬────────────────────────┘
                                 │
                    Static HTML dashboard (GitHub Pages)
```

Eight ingestion paths handle the reality that sources expose data differently — REST APIs (openFDA, ClinicalTrials.gov, Federal Register), native RSS/Atom, PubMed E-utilities, arXiv, curated Google-News queries for bodies with no feed, and lightweight HTML scraping. Everything runs on a schedule and renders to a single static page, so there is nothing to host and nothing to break at request time.

---

## Data sources

*Representative examples by layer — not the full list. The complete set of ~65 sources, the exact queries and the ranking configuration are maintained privately.*

| Layer | Sources |
|---|---|
| **Research** | arXiv (cs.AI / cs.LG / cs.CL), lab blogs, AI newsletters |
| **Clinical evidence** | ClinicalTrials.gov, NEJM AI, Lancet Digital Health, Nature Medicine, JAMIA, medRxiv, Ground Truths |
| **HEOR & HTA** | ICER, HTAi, INAHTA, HITAP, Value in Health, PharmacoEconomics, OHDSI, ISPOR, standing PubMed queries on AI × HTA |
| **Regulation** | FDA, EMA, MHRA, PMDA, NMPA, SFDA, Swissmedic, Health Canada (openFDA authorisations, US Federal Register rules) |
| **Reimbursement** | CMS, NICE, DiGA, NTAP/CPT, EU Joint Clinical Assessment, G-BA/IQWiG, HAS, CADTH, AIFA/TLV/Zorginstituut, HIRA, PBAC/MSAC |
| **Industry** | STAT, Endpoints, Fierce, MedTech Dive, MassDevice |

---

## Methodology

- **Deduplication** by exact URL — every item shown is a unique link.
- **Classification** into stage, jurisdiction (country → macro-region) and body (regulator / HTA-payer / professional society) is rule-based and auditable.
- **Ranking** follows explicit, additive rules (device authorisations, economic-endpoint trials, major-regulator actions, recency); it reflects priority, not confidence, and every item exposes its own "why ranked" breakdown.
- **Dates are never estimated.** Where a date can't be sourced it is recorded as `unknown` and excluded from any date-based figure.
- **No causal or predictive claims** are made beyond what the counts support, and company press releases are excluded to keep the feed independent.

Full definitions live in **[TAXONOMY.md](TAXONOMY.md)** — because "covered" is not one thing (a Category III CPT code, a provisional DiGA listing and a time-limited NTAP are not equivalent).

---

## Research dataset: clearance → coverage pathways

A separate, hand-verified dataset — not yet part of the main monitor — tracks how AI-enabled devices move from authorisation to reimbursement across major markets. It is intentionally conservative: entries are added only when dates can be verified from primary sources. The daily monitor remains the flagship.

---

## Potential extensions

The underlying dataset supports future analytics such as:

- regulatory landscape tracking
- reimbursement pathway monitoring
- evidence-gap analysis
- historical adoption trends
- enterprise alerts and API access

The core monitor remains focused on transparent evidence tracking.

---

## Stack

- Python ingestion pipeline
- GitHub Actions scheduled builds
- Static HTML / CSS / JavaScript frontend
- RSS feed generation
- GitHub Pages hosting

---

## Built with

AI-assisted development was used during creation; the running monitor itself uses no language model — all classification, ranking and dating are rule-based and reproducible.

## Licence & colophon

MIT licensed (see [LICENSE](LICENSE)). Static site, rebuilt daily by GitHub Actions — no tracking, analytics or cookies; read state is stored in the browser only. Contributions and corrections welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)). Maintained by [@asarmah123](https://github.com/asarmah123).
