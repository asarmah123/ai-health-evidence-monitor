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
        │  body) → deduplicate → rank → QA gate → render → │
        │  post-build validation → publish                 │
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

For news-style sources the real **publisher** is resolved for attribution (the curated query it was found through is kept separately), source tags are stripped from titles, and where a listing or feed carries no date the item's date is read from the **article page's own metadata** — still read from the source, never invented; anything without a usable date is shown as "date unknown".

Everything runs on a schedule and renders to a single static page, so there is nothing to host and nothing to break at request time.

## Pipeline

`fetch → normalise → classify (stage / region / body) → deduplicate (exact URL, then near-duplicate collapse) → rank → QA gate → render → post-build validation → publish`

- **Deduplicate** — exact-URL de-dup first, then a deterministic near-duplicate pass collapses the same event reported by several outlets (shared distinctive tokens + high title overlap), keeping the most complete item.
- **Classify / rank** — transparent, rule-based; no machine-learning model. See [TAXONOMY.md](../TAXONOMY.md).
- **QA gate** — required fields, a primary-source link and sane dates on every item, plus minimum source coverage; a build that fails is withheld and the previous validated build stays live.
- **Post-build validation** — an independent, deterministic pass recomputes every published figure from the final item set and reconciles it against the rendered page (integrity, scope, facets, home/analysis counts, empty-states, cross-page tags). It never blocks publication; it records a `validation.json` scoreboard and reports any discrepancy so issues are caught proactively.
