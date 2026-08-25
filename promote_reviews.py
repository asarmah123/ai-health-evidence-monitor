#!/usr/bin/env python3
"""Close the loop: a completed weekly review template → the gold sets, in one step.

This is the connective tissue between the human review and the two gold corpora, so they are never
maintained by hand independently. Given a review template whose rows a reviewer has filled in
(`verdict` = ok | wrong | ambiguous, `expect_*` confirmed/corrected, `reviewer`, `reviewed_on`,
optional `promote_to_frozen: true`), it:

  1. appends every REVIEWED row (any non-empty verdict) to classification_gold_rolling.json — with its
     stratum tags — so the rolling live-accuracy instrument grows;
  2. also appends rows marked `promote_to_frozen` to classification_gold.json — the permanent
     regression contract — for genuinely representative edge cases;
  3. reports the BOUNDARY-QUEUE HIT-RATE: of the boundary-flagged rows reviewed, what fraction the
     reviewer judged wrong-or-ambiguous. 5% ⇒ the queue is still noisy; 40–60% ⇒ a useful triage.
     A snapshot is appended to review_metrics.jsonl (append-only) to watch the rate over time.

De-duplicates by URL so re-running is safe. Nothing is overwritten.

  python promote_reviews.py --template review_template.filled.json
"""
import argparse, json, sys, datetime as dt

EXPECT = ["expect_stage", "expect_source_type", "expect_region", "expect_evidence_type",
          "expect_strength", "expect_decision_type", "expect_payer_type"]


def _load(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return {"items": []}


def _urls(doc):
    return {i.get("url") for i in doc.get("items", [])}


def _gold_row(r, reviewer, today):
    row = {"title": r.get("title", ""), "source": r.get("source", ""), "url": r.get("url", ""),
           "declared_layer": r.get("declared_layer", ""), "gnews": bool(r.get("gnews")),
           "stratum_stage": r.get("expect_stage", ""), "stratum_source_type": r.get("expect_source_type", ""),
           "reviewed_on": r.get("reviewed_on") or today, "reviewer": r.get("reviewer") or reviewer,
           "note": r.get("note", "")}
    for k in EXPECT:
        row[k] = r.get(k, "")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="the reviewer-completed review template JSON")
    ap.add_argument("--rolling", default="classification_gold_rolling.json")
    ap.add_argument("--frozen", default="classification_gold.json")
    ap.add_argument("--metrics", default="review_metrics.jsonl")
    ap.add_argument("--reviewer", default="weekly-review")
    a = ap.parse_args()
    today = dt.date.today().isoformat()

    tmpl = json.load(open(a.template, encoding="utf-8"))
    reviewed = [r for r in tmpl.get("items", []) if (r.get("verdict") or "").strip()]
    if not reviewed:
        print("no reviewed rows (fill in 'verdict' on the template first) — nothing to promote")
        return 0

    rolling = _load(a.rolling); r_urls = _urls(rolling)
    frozen = _load(a.frozen); f_urls = _urls(frozen)
    added_roll = added_frozen = 0
    for r in reviewed:
        u = r.get("url")
        if u not in r_urls:
            rolling.setdefault("items", []).append(_gold_row(r, a.reviewer, today))
            r_urls.add(u); added_roll += 1
        if r.get("promote_to_frozen") and u not in f_urls:
            frozen.setdefault("items", []).append(_gold_row(r, a.reviewer, today))
            f_urls.add(u); added_frozen += 1

    json.dump(rolling, open(a.rolling, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    if added_frozen:
        json.dump(frozen, open(a.frozen, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    # boundary-queue hit-rate: of reviewed BOUNDARY rows, how many judged wrong/ambiguous?
    bq = [r for r in reviewed if r.get("boundary")]
    flagged = [r for r in bq if (r.get("verdict") or "").strip().lower() in ("wrong", "ambiguous")]
    hit_rate = (len(flagged) / len(bq)) if bq else None
    with open(a.metrics, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": today, "reviewed": len(reviewed), "boundary_reviewed": len(bq),
                             "boundary_wrong_or_ambiguous": len(flagged),
                             "boundary_hit_rate": hit_rate}) + "\n")

    print(f"promoted {added_roll} → rolling gold, {added_frozen} → frozen regression gold")
    if hit_rate is not None:
        verdict = ("noisy — tighten boundary_reasons" if hit_rate < 0.15 else
                   "useful triage" if hit_rate >= 0.4 else "acceptable")
        print(f"boundary-queue hit-rate: {len(flagged)}/{len(bq)} = {hit_rate:.0%} ({verdict})")
    else:
        print("boundary-queue hit-rate: n/a (no boundary rows reviewed this cycle)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
