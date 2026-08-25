# Evaluation & quality system — Build A

How we know the monitor is right, and where we don't yet. The honest one-line summary:

> **Strong publication safety and regression protection; a maturing (not yet complete) measurement of
> live classification accuracy and recall.**

The system answers several *different* questions. Several parts are excellent at preventing internal
inconsistency without, on their own, telling you whether the underlying classification is correct — so
they are organised below by the question each one actually answers.

---

## Three quality systems, one feedback loop

```
                    ┌─────────────────────┐
                    │   PRE-PUBLISH       │
                    │ Golden regression   │
                    │ Mutation tests      │
                    │ Abort gates         │
                    └──────────┬──────────┘
                               ▼
                         PUBLISH BUILD
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        Integrity         Live precision     Recall
        validation        surveillance       probes
        (automated)       (human review)     (curated gold)
              └────────────────┼────────────────┘
                               ▼
                         FEEDBACK LOOP
                  ┌────────────┴────────────┐
                  ▼                         ▼
           Rolling gold             Permanent gold
           (live accuracy)          (regressions)
```

The feedback loop is the part that turns *"we have tests that stop yesterday's mistakes returning"*
into *"we continuously measure today's mistakes and feed representative failures back into the tests."*

---

## The six operational eval families

**1. Golden regression tests** — `tests/test_golden.py` (160+ tests incl. 22 mutation tests).
Run in CI *before* publish. Freeze the deterministic engine: if a rule changes behaviour on a fixed
fixture set, the build is skipped and the last good site stays live. Mutation tests prove each validator
check actually fires on the defect it targets. *Answers: did a change break known-good behaviour?*

**2. Abort gates** — `validate_or_abort` + `FAIL_ON_DEGRADE` (in `build.py`).
Hard stops that withhold the whole build: zero valid items, >X% of items failing QA, >20% of steady
feeds silent, missing/tiny `index.html`. *Answers: is this build safe to publish at all?*

**3. Per-build validation harness** — `validate_build.py` (~60 checks, alert-only, emailed every build).
Item integrity (E01–E18), scope leaks (S01–S06), facet vocabulary (F01–F04), home invariants (H01–H07),
analysis reconciliation (A01–A14), render reconciliation (R00–R05), empty-state/cross-page (Z/X).
*Answers: is the output well-formed, self-consistent, and does the rendered page match the data?*

**4. Classification-precision eval** — `P01` + `classification_gold.json` (frozen) and
`classification_gold_rolling.json` (rolling); grader `run_precision.py`.
Re-classifies human-reviewed fixtures through the SAME pipeline the site ships (`classify_for_eval`) and
grades **7 facets** (stage, source_type, region, evidence_type, strength, decision_type, payer_type),
with per-class precision/recall. The frozen set gates each build (P01) and catches regressions; the
rolling set measures whether *live* accuracy is drifting (append-only `precision_history.jsonl`).
*Answers: is the classification correct — and is that changing over time?*

**5. Low-confidence review queue** — `boundary_reasons()` + `check_boundary_queue`.
Active surveillance of the classifier's *least-certain* calls: stage reclassified during refinement,
ambiguous geography, competing stage signals, a news-type evidence-type in an evidence stage. Surfaced in
the build email so a human eyeballs exactly those, not random ordinary items. Requires no LLM.
*Answers: where should a human look first?*

**6. Recall / timeliness probe** — `evals/run_recall.py` (weekly, in the private repo).
Detection rate + median lag against a curated set of known events. Reported explicitly as **probe
recall** with a composition breakdown, because it is not a census. *Answers: of events we KNOW about,
how many (and how fast) did we catch?*

Supporting signals: **`verify_deploy.py`** (post-publish, confirms the live site reached the build) and
the **`native_url_pct`** operational metric in `build.json`.

---

## Architectural boundaries (design choices, not bugs)

These are stated so no eval creates a false impression of proving something it structurally cannot.

- **Title-based classification.** The engine classifies primarily from titles/metadata; article bodies
  are not read (summaries are promo teasers). Therefore: *classification accuracy is conditional on the
  information available in the source title/metadata.* The precision gold is title-based too — it proves
  rule correctness on the available signal, **not** article-level semantic correctness. This is a
  legitimate boundary, not a gap to paper over with another validator.

- **No language model.** All classification, ranking and dating are deterministic rules. This rules out
  an automated LLM judge for live precision — hence the human-review loop.

- **Google-News redirect destinations are unverifiable.** For gnews items the article URL isn't
  recoverable (decoder is IP-blocked). Checks E13–E15 prove provenance *coherence* (publisher present,
  homepage plausible, resolved-host matches) but cannot prove *redirect → the exact article named in the
  headline*. Rather than add validator complexity here, the strategic lever is **`native_url_pct`**:
  progressively reduce the unresolved-gnews fraction by adding native feeds.

- **Probe recall ≠ census recall.** The recall probe reports "we detected X of these known events," never
  "we capture X% of everything relevant." Expand it deliberately by coverage strata (specialty,
  geography, regulator, HTA body, evidence type, payer, journal/trial, industry) to *discover blind
  spots*, not to chase one magic number.

- **Date correctness vs plausibility.** E05/E16/E17 catch future/stale/implausible dates. Detecting a
  *wrong-but-plausible* date needs a defined canonical-date policy first (syndicated ≠ wrong), so it is
  deliberately deferred.

---

## What's covered vs pending

| Axis | State |
|---|---|
| Publication safety (won't ship a broken/gutted build) | **Strong** |
| Internal consistency / page-data reconciliation | **Strong** |
| Regression protection on known cases | **Strong** |
| Live classification accuracy (7 facets, per-class) | **Measured** — frozen gate + rolling trend + review queue |
| Recall / completeness | **Partial** — probe only; specialty/geography-biased; expand by strata |
| Google-News destination fidelity | **Structural limit** — coherence checked, destination not; track `native_url_pct` |
| Date correctness (beyond plausibility) | **Deferred** — needs a canonical-date policy |

---

## The weekly evaluation review (the loop, made explicit)

Run once a week (and after any rules change):

1. **Sample** new production items — `python sample_for_review.py --in docs/data/feed-latest.json`
   (stratified across stage × source_type, plus every boundary item, so rare categories aren't swamped).
2. **Review** the build emails' **low-confidence queue** for the week alongside the sample.
3. **Human-label** the sampled rows: set each `verdict` (`ok` | `wrong` | `ambiguous`), confirm or correct
   the pre-filled `expect_*` fields, and mark `promote_to_frozen: true` on genuinely representative edge cases.
4. **Promote in one step** — `python promote_reviews.py --template <filled>`: reviewed rows flow into
   `classification_gold_rolling.json` (and frozen candidates into `classification_gold.json`), de-duplicated,
   nothing overwritten. This is the automated boundary→corrected→rolling→frozen path — the two gold sets are
   never hand-maintained independently.
5. **Grade** — `python run_precision.py`; read overall + per-facet accuracy, per-class precision/recall,
   and the week-over-week delta (`precision_history.jsonl` is append-only).
6. **Watch the boundary-queue hit-rate** (`review_metrics.jsonl`, printed by `promote_reviews.py`): of the
   boundary-flagged rows reviewed, what fraction were judged wrong/ambiguous? **~5% ⇒ the queue is noisy**
   (tighten `boundary_reasons`); **40–60% ⇒ a useful triage.** The queue's *precision* matters as much as
   its recall — if reviewers stop trusting it, it stops being used.
7. **Recall:** add any missed known events to `evals/known_events.yaml`; where a whole stratum is thin,
   note it as a coverage blind spot to expand (the principal remaining evaluation weakness).
8. **Record** unresolved error categories so patterns (not just single items) get fixed at the rule level.

The two gold sets never merge: the frozen set is the regression memory; the rolling set is the live-
accuracy signal. Keeping them separate is what lets you see accuracy *change*, not just *not-regress*.
