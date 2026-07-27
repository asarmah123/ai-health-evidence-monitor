# AI in Health — Clinical and Market Access Evidence Monitor

**Live → https://asarmah123.github.io/ai-health-evidence-monitor/**

> **Daily market intelligence on how AI technologies advance through healthcare — from research and clinical validation through regulation, health technology assessment, reimbursement, and market adoption.**

`~65 curated sources` · `regulators & HTA bodies across 15+ markets` · `North America · Europe · APAC · MEA`

**AI in Health** pulls public signals from regulators, journals, trial registries and industry press into one briefing that refreshes every morning — framed the way a market-access team actually thinks, around two key questions: **can it be sold?** (authorisation) and **will it be paid for?** (coverage).

Built for **HEOR and market-access professionals** who need regulatory, evidentiary and payment signals in one place rather than five.

---

## Why trust it?

- **Primary sources first** — official regulator, HTA and registry APIs and feeds, not second-hand summaries.
- **Deterministic, rule-based classification** — every item's stage, region and body come from transparent rules (no model), and every ranking shows *why* it ranks where it does.
- **Dates are never inferred or fabricated** — they are read from the source, or shown as "date unknown."

Builds are reproducible: the same inputs produce the same output, rebuilt automatically every morning.

---

## Features

- **Follow AI from research through reimbursement** — the six-stage evidence journey in a single view.
- **See what matters first** — the day's top development and a ranked "worth a closer look," pulled to the top by explicit rule.
- **Spot what's unusual** — stage activity and term mentions compared against their own recent baseline.
- **Track the two gates** — *can it be sold?* (authorisation) vs *will it be paid?* (coverage).
- **Read leading indicators** — trials registering an economic endpoint, peer-reviewed value papers.
- **Compare activity across regions and countries** — regulators and HTA bodies across 15+ markets.
- **Explore an interactive feed** — search, sort and filter across the six stages.
- **Private by design** — a static site with no server, no tracking and no cookies.

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

| Layer | Sources |
|---|---|
| **Research** | arXiv (cs.AI / cs.LG / cs.CL), lab blogs, AI newsletters |
| **Clinical evidence** | ClinicalTrials.gov, NEJM AI, Lancet Digital Health, Nature Medicine, JAMIA, medRxiv, Ground Truths |
| **HEOR & HTA** | ICER, HTAi, INAHTA, HITAP, Value in Health, PharmacoEconomics, OHDSI, ISPOR, standing PubMed queries on AI × HTA |
| **Regulation** | FDA & CMS (Federal Register), EMA, MHRA (news + safety alerts), openFDA authorisations, PMDA/NMPA/SFDA/Swissmedic/Health Canada (via aggregators) |
| **Reimbursement** | CMS, NICE, DiGA, NTAP/CPT, EU Joint Clinical Assessment, G-BA/IQWiG, HAS, CADTH, AIFA/TLV/Zorginstituut, HIRA, PBAC/MSAC |
| **Industry** | STAT, Endpoints, Fierce, MedTech Dive, MassDevice |

---

## Methodology

- **Deduplication** by exact URL — every item shown is a unique link.
- **Classification** into stage, jurisdiction (country → macro-region) and body (regulator / HTA-payer / professional society) is rule-based and auditable.
- **Ranking** follows explicit, additive rules (device authorisations, economic-endpoint trials, major-regulator actions, recency); it reflects priority, not confidence, and every item exposes its own "why ranked" breakdown.
- **Dates are never estimated.** Where a date can't be sourced it is recorded as `unknown` and excluded from any date-based figure.
- **No causal claims** are generated beyond what the counts support, and company press releases are excluded to keep the feed independent.

Full definitions live in **[TAXONOMY.md](TAXONOMY.md)** — because "covered" is not one thing (a Category III CPT code, a provisional DiGA listing and a time-limited NTAP are not equivalent).

---

## Clearance → coverage dataset *(in progress)*

Alongside the feed, the project maintains a small, hand-verified longitudinal dataset of *how long AI-enabled devices take to go from authorisation to reimbursement* across major markets. It is intentionally conservative — it grows only when dates can be verified from primary sources, and both successful and unsuccessful outcomes (coverage, refusals, delistings) are retained to keep the record unbiased. It is an early, secondary component; the daily feed is the flagship.

---

## Stack

- **Ingestion** — Python (`feedparser`, `requests`, `BeautifulSoup`, `PyYAML`)
- **Automation** — GitHub Actions (scheduled daily build)
- **Frontend** — hand-written HTML / CSS / vanilla JavaScript (no framework, no runtime dependencies)
- **Hosting** — GitHub Pages (static)

---

## Roadmap

- Populate the clearance → coverage dataset with verified devices across US / EU / UK / DE
- Deepen the dataset — *what evidence won coverage* (study design, winning endpoint) per device
- Expand APAC/MEA ingestion as machine-readable feeds become available
- Optional AI "what matters today" briefing (a lightweight LLM pass over the day's items)

---

## Built with

This project was developed using **AI-assisted software engineering**. Large language models were used to accelerate design, implementation and documentation, while decisions about product scope, taxonomy, data sources, validation and review were made by the project author.

## Licence & colophon

MIT licensed (see [LICENSE](LICENSE)). Static site, rebuilt daily by GitHub Actions — no tracking, analytics or cookies; read state is stored in the browser only. Contributions and corrections welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)). Maintained by [@asarmah123](https://github.com/asarmah123).
