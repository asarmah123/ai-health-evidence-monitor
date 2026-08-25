#!/usr/bin/env python3
"""Stratified sampler for the weekly human-review loop.

Random sampling lets a large category (e.g. clinical journal studies) swamp rare-but-important ones
(coverage decisions, authorisations). This picks a spread ACROSS strata — stage x source_type — plus
every boundary/low-confidence item, and emits a review TEMPLATE: the classifier's current labels beside
blank `expect_*` fields for a human to confirm or correct. Corrected rows are the on-ramp to
classification_gold_rolling.json (and genuine edge cases can be promoted to the frozen regression gold).

  python sample_for_review.py --in docs/data/feed-latest.json --per-stratum 2 --out review_template.json
"""
import argparse, json, sys
from collections import defaultdict

try:
    import build
except Exception:
    build = None

FACETS = ["stage", "source_type", "region", "evidence_type", "strength", "decision_type", "payer_type"]


def load_items(path):
    d = json.load(open(path, encoding="utf-8"))
    return d.get("items", d if isinstance(d, list) else [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="docs/data/feed-latest.json")
    ap.add_argument("--per-stratum", type=int, default=2, help="items sampled per stage×source_type cell")
    ap.add_argument("--out", default="review_template.json")
    a = ap.parse_args()
    items = load_items(a.inp)

    # bucket by (stage, source_type); take up to N newest per cell (input is already importance-sorted)
    strata = defaultdict(list)
    for i in items:
        strata[(i.get("stage", ""), i.get("source_type", ""))].append(i)
    picked, seen = [], set()

    def _add(i, why):
        key = i.get("id") or i.get("url")
        if key in seen:
            return
        seen.add(key); picked.append((why, i))

    for (stg, sty), grp in sorted(strata.items()):
        for i in grp[: a.per_stratum]:
            _add(i, f"stratum {stg}/{sty}")
    # always include boundary / low-confidence items, even if their cell is already full
    if build is not None:
        for i in items:
            r = build.boundary_reasons(dict(i, stype=i.get("source_type"), etype=i.get("evidence_type"),
                                            layer=i.get("stage"), strength=i.get("evidence_strength")))
            if r:
                _add(i, "boundary: " + "; ".join(r))

    template = {"_comment": ["Weekly review template. For each row, confirm or correct the expect_* fields",
                             "(they are PRE-FILLED with the classifier's current labels). Move corrected/",
                             "representative rows into classification_gold_rolling.json with reviewer + date."],
                "sampled": len(picked), "of_total": len(items), "items": []}
    for why, i in picked:
        template["items"].append({
            "why_sampled": why,
            "boundary": why.startswith("boundary:"),          # was this flagged by the low-confidence queue?
            "verdict": "",                                     # reviewer fills: ok | wrong | ambiguous
            "title": i.get("title", ""), "source": i.get("source", ""), "url": i.get("url", ""),
            "current": {f: i.get(f if f != "source_type" else "source_type", i.get("evidence_strength") if f == "strength" else "")
                        for f in FACETS},
            **{f"expect_{f}": i.get(f, "") for f in ("stage", "source_type", "region")},
            "expect_evidence_type": i.get("evidence_type", ""),
            "expect_strength": i.get("evidence_strength", ""),
            "expect_decision_type": i.get("decision_type", ""),
            "expect_payer_type": i.get("payer_type", ""),
            "reviewed_on": "", "reviewer": "", "note": "",
        })
    json.dump(template, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"wrote {a.out}: {len(picked)} items sampled across {len(strata)} strata (of {len(items)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
