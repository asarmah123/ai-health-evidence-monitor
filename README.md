# AI in Health — Clinical and Market Access Evidence Monitor

**Live → https://asarmah123.github.io/ai-heor-feed/**

> **One morning briefing for AI across the entire market-access lifecycle — research to reimbursement — built around the only two questions that decide adoption: _can it be sold, and will it be paid for?_**

`~60 curated sources` · `8 ingestion paths` · `regulatory & HTA bodies across 15+ markets` · `North America · Europe · APAC · MEA`

**AI in Health** is a daily intelligence monitor that tracks artificial intelligence across the
full pipeline a health technology travels — from **research** and **clinical evidence**, through
**regulatory authorisation** and **health technology assessment (HTA)**, to **reimbursement** and
**market access**. It pulls public signals from regulators, academic literature, clinical-trial
registries and industry press into one briefing that refreshes every morning, and it maintains a
longitudinal dataset of *how long AI-enabled devices actually take to go from authorisation to
reimbursement* across major markets.

It's built for a specific audience: **HEOR and market-access professionals** — the people whose
job is to move AI from proof-of-concept to reimbursed, scaled use, and who need regulatory,
evidentiary and payment signals in one place rather than five.

---

## Status

Active research project. The clearance → coverage dataset is intentionally conservative and
expands only when dates can be verified from primary sources. Some regional panels (APAC, MEA)
are sparse because most HTA bodies in those regions do not publish machine-readable English feeds.

---

## Why?

AI adoption in healthcare depends on more than model performance. A product moves through
research, clinical validation, regulatory review, HTA assessment and reimbursement — and the
signals that matter at each stage are scattered across dozens of organisations, journals,
registries and regulators.

**AI in Health** brings them into a single daily briefing, framed the way a market-access team
actually thinks: *can it be sold, and will it be paid for?*

---

## Features

- **Daily briefing** — rebuilt every morning by an automated job; no manual step
- **Six-stage pipeline view** — research · clinical · HEOR · regulation · reimbursement · industry
- **"Two gates" framing** — *can it be sold?* (authorisation) vs *will it be paid?* (coverage)
- **Leading indicators** — AI trials registering an economic endpoint, peer-reviewed HTA/value papers
- **Regulatory & HTA monitoring** — FDA, EMA, NICE, CMS, ISPOR, plus APAC/MEA bodies (PMDA, HITAP, HIRA, SFDA…)
- **Geographic lens** — activity by macro-region and country
- **Clearance → coverage analytics** — median days from authorisation to reimbursement, by market
- **Interactive feed** — tiered (daily/weekly/monthly), filterable across six categories
- **Static deployment** — no server, no tracking, no cookies; read-state stored in the browser only

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
        │  body) → deduplicate (by URL) → tag              │
        └────────────────────────┬────────────────────────┘
                                 │
             Evidence dataset  +  clearance→coverage dataset
                                 │
                    Static HTML dashboard (GitHub Pages)
```

Five ingestion paths handle the reality that different sources expose data differently — REST
APIs (openFDA, ClinicalTrials.gov, Federal Register), RSS, PubMed E-utilities, curated news
queries, and HTML scraping for bodies with no feed. Everything runs on a schedule; the output is
a single static page, so there is nothing to host and nothing to break at request time.

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
- **Classification** into stage, jurisdiction (country → macro-region), and body (regulator /
  HTA-payer / professional society) is rule-based and auditable.
- **Clearance → coverage** definitions are documented in **[TAXONOMY.md](TAXONOMY.md)** — because
  "covered" is not one thing (a Category III CPT code, a provisional DiGA listing, and a time-
  limited NTAP are not equivalent).
- **Dates are never estimated.** Where a date can't be sourced it is recorded as `unknown` and
  excluded from medians. Both successful and unsuccessful outcomes — coverage, refusals,
  delistings — are retained, to preserve an unbiased historical record.

The dataset is deliberately honest about its limits: coverage figures are based on a small,
growing set of devices, and APAC/MEA coverage is thin for the reason noted under **Status**.

---

## Stack

- **Ingestion** — Python (`feedparser`, `requests`, `BeautifulSoup`, `PyYAML`)
- **Sources** — REST APIs · RSS · PubMed E-utilities · curated news queries · HTML scraping
- **Automation** — GitHub Actions (scheduled daily build)
- **Frontend** — hand-written HTML / CSS / vanilla JavaScript (no framework, no dependencies)
- **Hosting** — GitHub Pages (static)

---

## Roadmap

- Populate the clearance → coverage dataset with verified devices across US / EU / UK / DE
- Deepen the dataset — *what evidence won coverage* (study design, winning endpoint) per device
- Expand APAC/MEA ingestion as machine-readable feeds become available
- Optional AI "what matters today" briefing (a lightweight LLM pass over the day's items)

---

## Built with

This project was developed using **AI-assisted software engineering**. Large language models were
used to accelerate design, implementation and documentation, while decisions about product scope,
taxonomy, data sources, validation and review were made by the project author.

## Licence & colophon

MIT licensed (see [LICENSE](LICENSE)). Static site, rebuilt daily by GitHub Actions — no tracking,
analytics or cookies; read state is stored in the browser only. Contributions and corrections
welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)). Maintained by
[@asarmah123](https://github.com/asarmah123).
