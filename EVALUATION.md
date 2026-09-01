# Evaluation & quality system — Build A

Authoritative reference for how the AI-in-Health evidence monitor is evaluated: every eval described in
place (file + function), how the whole system fits together, and what remains to be done.

**One-line status:** *Publication safety and regression protection are mature; live-classification
measurement is operational but still needs several weekly cycles to establish a reliable trend; recall
measurement is useful but exploratory. The main next investment is broadening the recall probe and
running the review loop — not adding more generic validators.*

> Note on "operational": the machinery for live-precision measurement is credible and tested, but the
> empirical measurement *history* is not yet established — one seeded rolling set, no accumulated weekly
> cycles. Treat rolling-gold numbers as a starting baseline, not a validated longitudinal estimate.

The system answers several **different** questions. Several parts are excellent at preventing internal
inconsistency without, on their own, telling you whether the underlying classification is correct — so
each is labelled below by the question it actually answers.

---

## Architecture

```
                    ┌─────────────────────┐
                    │   PRE-PUBLISH       │
                    │                     │
                    │ Golden regression   │
                    │ Mutation tests      │
                    │ Abort gates         │
                    └──────────┬──────────┘
                               │
                               ▼
                         PUBLISH BUILD
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        Integrity         Live precision     Recall
        validation        surveillance       probes
              │                │                │
          automated       human review       curated gold
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                         FEEDBACK LOOP
                               │
                  ┌────────────┴────────────┐
                  ▼                         ▼
           Rolling gold             Permanent gold
           / edge cases             / regressions
```

Three quality systems, one feedback loop. The loop is what turns *"we have tests that stop yesterday's
mistakes returning"* into *"we continuously measure today's mistakes and feed representative failures
back into the tests."*

| Layer                  | What it answers                                 | Status                           |
| ---------------------- | ----------------------------------------------- | -------------------------------- |
| Golden regression      | Did known behavior change?                      | **Strong**                       |
| Abort gates            | Is this build safe to publish?                  | **Strong**                       |
| Build validation       | Is the output internally coherent?              | **Strong**                       |
| Frozen precision gold  | Did known classification behavior regress?      | **Strong**                       |
| Rolling precision gold | Can we measure live classification accuracy and detect drift? | **Operational; not yet validated over time** |
| Boundary queue         | Where should humans look first?                 | **Now operational**              |
| Probe recall           | Are known events being detected?                | **Operational, limited breadth** |
| Native URL %           | Are we reducing GNews dependence?               | **Operational**                  |
| Deploy verification    | Did the tested build actually reach production? | **Strong**                       |

---

## PRE-PUBLISH — run before anything is published; a failure withholds the build

### Golden regression tests — *Did known behaviour change?*
- **In place:** `tests/test_golden.py` (164 tests); CI step "Golden regression tests" in
  `.github/workflows/daily.yml`, run **before** `build.py`.
- **How it works:** runs the real ranking, dedup, classification and history functions over a fixed
  fixture set and asserts byte-for-byte on the results. Any change to a ranking weight, topic rule or
  taxonomy label breaks a test.
- **On failure:** the CI job stops, `build.py` never runs, and the previous validated site stays live —
  so drift can never publish silently. A deliberate rule change requires consciously re-freezing the
  baseline and bumping `TAXONOMY_VERSION`.

### Mutation tests — *Do the validators actually fire?*
- **In place:** 22 `test_mut_*` / sensitivity tests inside `tests/test_golden.py`.
- **How it works:** each injects a synthetic defect (duplicate URL, future date, a paywalled featured
  item, a stale-date cluster, a residual duplicate, a mislabeled gold row…) and asserts the corresponding
  check raises its code. This proves the alarm wiring works — a validator that never fires is worthless.

### Abort gates — *Is this build safe to publish at all?*
- **In place:** `validate_or_abort()` and the `FAIL_ON_DEGRADE` path in `build.py`; the empty/too-small
  `index.html` guard.
- **How it works:** hard stops (non-zero exit, no publish) on systemic failure — zero valid items after
  QA, more than the allowed fraction of items failing QA (data corruption), >20% of steady feeds going
  silent (source-coverage collapse), or a missing/tiny rendered page. There is also an opt-in
  `VALIDATE_SELFTEST` that injects a defect into validation only, to prove the alert path end-to-end.

---

## PUBLISH → INTEGRITY VALIDATION (automated) — *Is the output internally coherent?*

- **In place:** `validate_build.py`, `run_validation()` — ~60 checks, **alert-only** (runs after publish
  and emails a report every build; it never blocks a publish that already happened). Written to
  `docs/validation.json` + `validation_report.{md,html}`; emailed via `daily.yml`.
- **The check families (code prefixes):**
  - **E01–E18** *item integrity* — required fields, valid/​unique URLs and ids, sane stages, in-vocab
    evidence types, sampled dead-link probe, internal routes, Google-News prevalence (E12), plus the
    provenance/date/dedup checks: publisher present on gnews items (E13), `publisher_url` is a homepage
    not an article (E14), `resolved_url` host matches publisher (E15), implausibly-old/materially-stale
    dates (E16/E17), same-headline residual duplicate (E18).
  - **S01–S06** *scope leaks* — drug-approval leak, out-of-scope (vet/agri/materials), commentary present,
    research-layer item not biomedical/health.
  - **F01–F04** *facet vocabulary* — strength / relevance / modality / maturity must be selectable values.
  - **H01–H07** *home invariants* — featured item is never litigation/drug/reference-guide/gnews; the
    Home metric tiles reconcile to recomputed counts; ranking order holds.
  - **A01–A14** *analysis reconciliation* — every Analysis figure (stage sum, region/country, authorisation
    undercount, FDA-feed match, trial/econ denominators, specialty, HEOR, coverage gate, body attribution)
    recomputed from the items and compared.
  - **R00–R05** *render reconciliation* — the **rendered HTML** and its embedded JSON match the data
    (counts, ids, shapes, featured/top rows, facet values, metric tiles).
  - **Z / X** — empty-state consistency and cross-page topic-tag agreement.
- **What it does NOT do:** judge whether a *stage/type is correct* — only that the output is well-formed,
  in-vocabulary, and self-consistent with the page. A mislabeled-but-well-formed item passes every check
  here. That gap is the job of the precision layer below.

---

## PUBLISH → LIVE PRECISION SURVEILLANCE (human review) — *Is classification correct, and drifting?*

Four cooperating pieces. The two gold sets are deliberately **separate and never merged**: the frozen set
is the stable regression *contract*; the rolling set is the evolving measurement *instrument*.

### Frozen precision gold — *Did known classification behaviour regress?*
- **In place:** `classification_gold.json` (76 human-reviewed items); graded live by
  `validate_build.py::check_classification_precision` (**P01**) and frozen by
  `tests/test_golden.py::test_classification_precision_gold_set`.
- **How it works:** re-classifies each fixture through the *same* pipeline the site ships
  (`build.classify_for_eval` → `apply_classification_refiners`) and grades **7 facets** — stage,
  source_type, region, evidence_type, strength, decision_type, payer_type. P01 warns if any facet drops
  below threshold (95% core / 90% sparse) and names the regressed item. Also grades an `(excluded)`
  outcome, so opinion-exclusion is a first-class tested result.
- **Status: Strong.** Every failure mode found in audits (governance→clinical, gnews→Regulator,
  viewpoint→Primary, opinion-column→authorisation, Bulgaria geo) is frozen here.

### Rolling precision gold — *Can we measure live classification accuracy and detect drift?*
- **In place:** `classification_gold_rolling.json` (grows weekly; strata-tagged, reviewer + date per row);
  grader `run_precision.py`; append-only history `precision_history.jsonl`.
- **How it works:** `run_precision.py` grades **both** gold sets, reporting overall + per-facet accuracy,
  **per-class precision/recall** (stage & source_type), and sample size; appends one snapshot per run and
  prints the week-over-week delta. Frozen regression on a core facet exits non-zero; the rolling set is
  measurement-only.
- **Why separate:** keeping the two apart is what lets you see accuracy *change*, not merely *not-regress*.

### Boundary queue — *Where should humans look first?*
- **In place:** `build.boundary_reasons()` + `apply_classification_refiners(trace=True)` (records each
  item's `_refine_trail`); surfaced by `validate_build.py::check_boundary_queue` into the emailed report
  (`Report.review_queue`, rendered in both markdown and HTML). Internal `_`-prefixed trace keys are
  stripped before export/embedding.
- **How it works:** flags only genuine uncertainty — stage reclassified during refinement (with the exact
  refiner hops), ambiguous geography (2+ country tokens), competing stage signals, a news-type
  evidence-type sitting in an evidence stage. It deliberately does **not** flag routine gnews→`Other`
  provenance (expected, tracked by E12 + native-URL %) — that noise is what makes a queue distrusted.
- **Its own precision matters:** the promotion step records a **boundary-queue hit-rate** — of boundary
  rows reviewed, the fraction judged wrong/ambiguous. ~5% ⇒ noisy (tighten `boundary_reasons`);
  40–60% ⇒ useful triage.

### The weekly loop tooling (the feedback loop, mechanised)
- **`sample_for_review.py`** — stratified sampler across stage × source_type + every boundary item, so
  rare categories aren't swamped; emits a pre-filled review template with blank `expect_*` + `verdict`.
- **`promote_reviews.py`** — one-step promotion, de-duplicated, nothing overwritten; writes the hit-rate
  to `review_metrics.jsonl`. The two gold sets are never hand-maintained independently.

  **Promotion rule (preserves the rolling set's statistical meaning):**
  - **All** sampled, sufficiently-reviewed items (verdict `ok` **and** `wrong`/`ambiguous`) → eligible for
    the **rolling gold**. This is deliberate: if only errors entered, the rolling corpus would become
    enriched for failures and stop representing production, so its accuracy % would no longer estimate
    live precision. Representative `ok` cases are what keep it a valid sample.
  - **Representative errors / edge cases** (`promote_to_frozen: true`) → *additionally* eligible for the
    **frozen gold** (a permanent regression contract).
  - **Boundary-queue items** → tracked **separately** for hit-rate analysis (they are a triage signal, not
    a random sample, so they must not silently skew the rolling accuracy estimate — review them, promote
    them like any other reviewed row, but read the hit-rate on its own).

---

## PUBLISH → RECALL PROBES (curated gold) — *Are known events being detected?*

- **In place:** `evals/run_recall.py` + `evals/build_a/known_events.yaml` (~32 events), run **weekly** in
  the private data repo over the append-only `feed-log.jsonl`. Read-only; never touches the
  build.
- **How it works:** for each recent, monitorable gold event, checks whether a corpus item matched within
  the detection window; reports **probe recall** (detected / scoreable, excluding pre-log events) and
  median detection lag, plus a composition breakdown by specialty/type.
- **Honest framing:** it reports "we detected X of *these known* events," **never** "we capture X% of
  everything relevant." It is a probe, not a census.
- **Status: Operational, limited breadth** — the gold set is small and cardiology/US-leaning, so
  per-specialty and ex-US recall are effectively unmeasured. **This is the principal remaining weakness.**

---

## SUPPORTING SIGNALS

### Native URL % — *Are we reducing Google-News dependence?*
- **In place:** `native_url_pct` (+ `gnews_redirect_items`) in `docs/build.json`.
- **Why:** the ~30% of items reached via a Google-News redirect have an unrecoverable article URL. Rather
  than add validator complexity for something structurally unverifiable, this is the operational lever:
  grow the native-URL share by adding native feeds.

### Deploy verification — *Did the tested build actually reach production?*
- **In place:** `verify_deploy.py` + the "Verify live deploy is fresh" step in `daily.yml`.
- **How it works:** after publish, polls the live `build.json` and confirms it reached the just-built
  `generated_at` + `taxonomy_version` (Pages deploys asynchronously and can silently lag). Alert-only.

---

## FEEDBACK LOOP — where failures become tests

```
production build → boundary queue + stratified sample → human verdicts (sample_for_review)
      → promote_reviews.py → rolling gold (edge cases)  ── measures live drift (run_precision)
                           → frozen gold (regressions)  ── locks the contract (P01 + golden test)
```

**Every** sufficiently-reviewed sampled item — correct *and* corrected — becomes a **rolling-gold** row, so
the rolling set stays a representative sample and its accuracy % remains a valid live-precision estimate;
a genuinely representative *error/edge case* is *additionally* promoted to the **frozen gold** (a permanent
regression). The frozen and rolling sets stay separate on purpose.

---

## Architectural boundaries (design choices, not defects)

Stated so no eval implies it proves something it structurally cannot:

- **Title-based classification.** Article bodies are not read (summaries are promo teasers). *Classification
  accuracy is conditional on the title/metadata available* — and the precision gold, being title-based,
  proves rule correctness on that signal, not article-level semantic correctness. Not a gap to paper over
  with another validator.
- **No language model.** All classification/ranking/dating is deterministic rules — which is *why* live
  precision needs a human-review loop rather than an automated judge.
- **Google-News destinations unverifiable.** E13–E15 prove provenance *coherence*; they cannot prove
  *redirect → the exact headlined article* (decoder is IP-blocked). The lever is `native_url_pct`.
- **Probe recall ≠ census recall.** Expand the probe to *discover blind spots*, not to chase one number.
- **Date correctness vs plausibility.** E05/E16/E17 catch future/stale/implausible dates; detecting a
  *wrong-but-plausible* date needs a defined canonical-date policy first (syndicated ≠ wrong) — deferred.

---

## What needs to be done

Ordered by value; deliberately **not** "add another eval subsystem."

1. **Run several weekly review cycles** (the runbook below) and let evidence accumulate:
   `precision_history.jsonl` trend, the boundary-queue hit-rate, and which strata the recall probe keeps
   missing. That evidence — not guesswork — should drive the next engineering.
2. **Broaden the recall probe by strata** — the principal open weakness. Expand `known_events.yaml`
   across: regulator × geography, HTA × geography, specialty, journal/trial, reimbursement/payer, evidence
   type, and emerging AI/device activity. Goal: surface **coverage asymmetries**, not manufacture a
   flattering global recall %.
3. **Watch the boundary-queue precision.** If the hit-rate sits near 5%, tighten `boundary_reasons`; if
   40–60%, it's a healthy triage. The queue's precision matters as much as its recall.
4. **(Optional plumbing)** a weekly GitHub Actions job that runs `run_precision.py` and emails the report,
   mirroring the recall job — only if the manual cadence proves inconvenient.
5. **Resist** adding further generic validation rules for now. Publication safety, regression protection
   and internal coherence are mature; effort is better spent on recall breadth and on the review cadence.

### Weekly evaluation review (the runbook)

1. **Sample** — `python sample_for_review.py --in docs/data/feed-latest.json` (stratified + all boundary items).
2. **Review** the week's build-email **boundary queues** alongside the sample.
3. **Label** each row: set `verdict` (`ok` | `wrong` | `ambiguous`), confirm/correct `expect_*`, mark
   `promote_to_frozen: true` on representative edge cases.
4. **Promote** — `python promote_reviews.py --template <filled>`: **every** reviewed row (incl. `ok`) →
   rolling gold; `promote_to_frozen` rows → *also* frozen gold; de-duplicated, nothing overwritten.
5. **Grade** — `python run_precision.py` (overall + per-facet + per-class + week-over-week delta).
6. **Watch** the boundary-queue hit-rate in `review_metrics.jsonl`.
7. **Recall** — add missed known events to `evals/known_events.yaml`; note thin strata as blind spots.
8. **Record** unresolved error *categories* so patterns get fixed at the rule level, not item by item.
