# Classification taxonomy

*AI in Health — Clinical, Regulatory & Market Access Evidence Monitor. Taxonomy version **2.52**
(stamped in `build.json` and every export). Every label below is assigned by explicit,
deterministic rules over source, terminology and lifecycle signals — **no machine-learning model** —
so identical inputs always produce identical output and historical comparisons stay valid.*

Definitions are published openly and may be cited or adopted. If you disagree with a rule, open an
issue — the rules improve by being argued with.

---

## 1. Evidence stage (`stage`)

The six-stage lifecycle an AI technology moves through. Each item is assigned exactly one stage.

| Stage | Question it answers | What belongs here |
|---|---|---|
| **Research & evidence** (`research`) | *Can AI do this?* | Frontier AI models, methods, benchmarks and evaluation techniques directly relevant to healthcare/biomedical AI. Generic AI with no material biomedical focus is excluded. |
| **Clinical evidence & trials** (`clinical`) | *Does it work in patients?* | Clinical studies, trials and real-world evaluations of AI performance, safety or effectiveness in healthcare. |
| **Regulatory, safety & authorisation** (`regulation`) | *Can it be safely deployed and authorised?* | Regulatory guidance, AI-governance and safety expectations, and AI-enabled medical-device authorisations. |
| **HEOR, HTA & value** (`heor`) | *How is the value assessed?* | Health technology assessment, health economics, cost-effectiveness, budget impact and value frameworks. |
| **Market access, reimbursement & coverage** (`access`) | *How does it reach practice?* | Coverage decisions, reimbursement policy, procurement/commissioning and payer guidance. |
| **Industry, investment & partnerships** (`industry`) | *Who is doing business?* | Commercial strategy, investment/M&A, partnerships, product launches and enterprise adoption involving a commercial actor. |

A precision-refinement pipeline corrects boundary cases after the first-pass stage — e.g. a
News/VC/commercial item never sits in an evidence stage; a regulator/HTA body's *safe-AI-adoption*
plan routes to Regulation; a policy/opinion "review" is treated as commentary and excluded; and
out-of-scope items (pharmaceutical drug approvals, veterinary/agriculture/materials science) are dropped.

---

## 2. Evidence type (`evidence_type`)

The kind of item, from NLM publication type (most reliable, PubMed only), then preprint provenance,
then study-design / regulatory / commercial signals.

- **Study / evidence:** `Journal study` · `Preprint` (arXiv/medRxiv/bioRxiv provenance only) · `Review` · `Systematic review` · `Meta-analysis` · `RCT` · `Trial registry` · `Real-world evidence` · `Study protocol`
- **Value / HEOR:** `Economic evaluation` · `HEOR / value` · `HTA report` · `HTA perspective` · `Value framework` · `Budget impact` · `Methodology`
- **Regulatory:** `Regulatory authorisation` · `Regulatory guidance` · `Regulatory programme` · `Rule / legislation` · `Enforcement / safety` · `Consultation / policy` · `AI governance`
- **Access:** `Payment / coverage` · `Market access`
- **Industry:** `Deployment` · `Product launch` · `Partnership` · `Executive move` · `Acquisition` · `Funding round` · `Company strategy` · `Industry news` · `Industry analysis`
- **Cross-cutting:** `News` · `Legal / litigation` · `Commentary` (commentary is excluded from the feed)

*Type rules of note:* `Executive move` requires a genuine appointment/departure (not a C-suite title
in an interview); a journal-published paper is a `Journal study`, never a `Preprint`.

---

## 3. Evidence strength (`evidence_strength`)

How much weight the item's content carries.

`Primary evidence` · `Secondary evidence` · `Policy signal` · `Market signal` · `Commentary`

---

## 4. Facets

| Facet | Values |
|---|---|
| **Healthcare relevance** (`healthcare_relevance`) | `Direct clinical` · `Healthcare operations` · `Biomedical research` · `Adjacent AI` · `General AI` |
| **AI modality** (`ai_modality`) | `Imaging AI` · `Generative AI / LLM` · `Clinical decision support` · `Digital therapeutic` · `Predictive ML` · `Remote monitoring` · `Robotics` · `Drug discovery AI` |
| **Evidence maturity** (`evidence_maturity`) | `Discovery` → `Retrospective` → `Prospective` → `Randomised` → `Real-world` → `Synthesis` → `Economic model` → `HTA` → `Value evidence` |

Facets are automated triage signals, not authoritative determinations, and may contain errors.

---

## 5. Jurisdiction (`country`, `region`)

Country read from structured metadata (a trial's location, a regulator's jurisdiction) or a
distinctive body/geography term — never inferred from a news query's target country. Country maps to
a macro-region:

**North America · Europe · Asia-Pacific · Latin America · Middle East & Africa**

---

## 6. Body role

Named bodies are split by the decision they make: **regulator** (gates market authorisation) ·
**HTA / payer** (gates reimbursement) · **professional society** (sets standards, no binding
decision). Matched by source and, for distinctive acronyms only, free text.

---

## 7. Clinical area

Specialty tagged by keyword: Radiology & imaging · Cardiology · Oncology · Ophthalmology ·
Pathology · Neurology · Gastroenterology · Dermatology · Mental health · Endocrine / diabetes ·
Pulmonology.

---

## 8. Reimbursement pathway

The access route a coverage-relevant item concerns (keyword-tagged): NTAP · CPT / coding · DiGA ·
PECAN · NICE EVA · LCD / MAC · Reimbursement (general).

---

## 9. Ranking & trends

- **Ranking** is additive and self-explaining — explicit signals (device authorisations,
  economic-endpoint trials, major-regulator actions, recency) sum to a score, and each item exposes
  its own "why ranked" breakdown. Ranking reflects **priority, not confidence**.
- **Trends** compare each tracked term to its own trailing 28-day baseline, framed as **attention,
  not importance**; term counts use a leading word-boundary match so "agent" isn't inflated by "reagent".
- **No causal or predictive claims** — the monitor reports *what changed and how unusual it is versus
  a baseline*, never *why* or *what's next*.

---

*When a classification or ranking rule changes, `TAXONOMY_VERSION` is bumped and this document is
updated. Every published build stamps the version so the past is never silently reclassified.*
