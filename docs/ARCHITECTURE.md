# Architecture


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

## Ingestion paths

Eight ingestion paths handle the reality that sources expose data differently:

- REST APIs — openFDA, ClinicalTrials.gov, Federal Register
- native RSS / Atom feeds
- PubMed E-utilities
- arXiv API
- curated Google-News queries for bodies with no machine-readable feed
- lightweight HTML scraping (visible dates read, never invented)

Everything runs on a schedule and renders to a single static page, so there is nothing to host and nothing to break at request time.

## Pipeline

`fetch → normalise → classify (stage / region / body) → deduplicate (exact URL, then near-duplicate collapse) → rank → render`

- **Deduplicate** — exact-URL de-dup first, then a deterministic near-duplicate pass collapses the same event reported by several outlets (shared distinctive tokens + high title overlap), keeping the most complete item.
- **Classify / rank** — transparent, rule-based; no machine-learning model. See [TAXONOMY.md](../TAXONOMY.md).
