#!/usr/bin/env python3
"""Classification-precision scorer over the gold sets — the measurement half of the accuracy loop.

Two gold sets, deliberately separate (see their headers):
  - classification_gold.json          FROZEN regression corpus (also gated live by validate_build P01)
  - classification_gold_rolling.json  ROLLING, human-reviewed production samples that GROW weekly

For each set this re-classifies every fixture through the SAME pipeline the build ships
(build.classify_for_eval) and reports: overall accuracy, per-facet accuracy, per-CLASS precision/recall
(for stage and source_type), and sample size. It appends one snapshot per run to precision_history.jsonl
(never overwriting) so drift across weeks is visible, and prints the delta vs the previous run.

This is measurement, not a gate — it tells you whether LIVE classification accuracy is moving, which the
frozen regression suite alone cannot. Run weekly (or after a rules change):
  python run_precision.py --out precision_report.md
"""
import argparse, json, sys, datetime as dt
from collections import Counter, defaultdict

import build  # the classifier under test — same pipeline the site ships

FACETS = ["stage", "source_type", "region", "evidence_type", "strength", "decision_type", "payer_type"]
SPARSE = {"region", "decision_type", "payer_type"}          # graded only where an expected value is set
PER_CLASS = ["stage", "source_type"]                        # facets we break down by class


def _load(path):
    try:
        return json.load(open(path, encoding="utf-8")).get("items", [])
    except FileNotFoundError:
        return None


def grade(gold):
    """Return per-facet accuracy + per-class precision/recall + mismatches for one gold set."""
    tot = {f: 0 for f in FACETS}; ok = {f: 0 for f in FACETS}
    pred = defaultdict(Counter); truth = defaultdict(Counter); hit = defaultdict(Counter)
    mism = []
    for g in gold:
        raw = {"title": g.get("title", ""), "source": g.get("source", ""), "url": g.get("url", ""),
               "declared_layer": g.get("declared_layer", ""), "gnews": bool(g.get("gnews"))}
        got = build.classify_for_eval(raw)
        for f in FACETS:
            exp = g.get("expect_" + f, "")
            if f in SPARSE and not exp:
                continue
            tot[f] += 1
            g_val = got.get(f)
            if g_val == exp:
                ok[f] += 1
            else:
                mism.append((f, g.get("title", "?")[:44], g_val, exp))
            if f in PER_CLASS:
                pred[f][g_val] += 1; truth[f][exp] += 1
                if g_val == exp:
                    hit[f][exp] += 1
    acc = {f: (ok[f] / tot[f] if tot[f] else None) for f in FACETS}
    per_class = {}
    for f in PER_CLASS:
        classes = sorted(set(pred[f]) | set(truth[f]))
        per_class[f] = {c: {
            "precision": (hit[f][c] / pred[f][c]) if pred[f][c] else None,
            "recall": (hit[f][c] / truth[f][c]) if truth[f][c] else None,
            "n_expected": truth[f][c],
        } for c in classes}
    return {"n": len(gold), "tot": tot, "ok": ok, "accuracy": acc,
            "per_class": per_class, "mismatches": mism}


def _pct(x):
    return "n/a" if x is None else f"{100*x:.0f}%"


def render(name, res):
    L = [f"### {name} — {res['n']} items", ""]
    L.append("| facet | accuracy | graded |")
    L.append("|---|---|---|")
    for f in FACETS:
        L.append(f"| {f} | {_pct(res['accuracy'][f])} | {res['ok'][f]}/{res['tot'][f]} |")
    L.append("")
    for f in PER_CLASS:
        L.append(f"**Per-class {f} (precision / recall · n):**")
        for c, s in res["per_class"][f].items():
            L.append(f"- `{c}` — P {_pct(s['precision'])} / R {_pct(s['recall'])} · n={s['n_expected']}")
        L.append("")
    if res["mismatches"]:
        L.append("**Mismatches (facet · item · got → expected):**")
        for f, t, g, e in res["mismatches"][:20]:
            L.append(f"- {f}: {t} — {g!r} → {e!r}")
        L.append("")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", default="classification_gold.json")
    ap.add_argument("--rolling", default="classification_gold_rolling.json")
    ap.add_argument("--history", default="precision_history.jsonl")
    ap.add_argument("--out", default="precision_report.md")
    a = ap.parse_args()

    sets = {}
    for name, path in (("frozen-regression", a.frozen), ("rolling-live", a.rolling)):
        gold = _load(path)
        if gold is None:
            print(f"(skip {name}: {path} not found)")
            continue
        sets[name] = grade(gold)

    today = dt.date.today().isoformat()
    L = [f"# Classification precision — {today}", ""]
    for name, res in sets.items():
        L += render(name, res)

    # history: append one snapshot per run (NEVER overwrite — the point is week-over-week drift)
    snap = {"date": today, "sets": {n: {"n": r["n"],
             "accuracy": {f: r["accuracy"][f] for f in FACETS}} for n, r in sets.items()}}
    prev = None
    try:
        lines = [l for l in open(a.history, encoding="utf-8") if l.strip()]
        if lines:
            prev = json.loads(lines[-1])
    except FileNotFoundError:
        pass
    with open(a.history, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap) + "\n")

    if prev:
        L.append("### Change vs previous run (" + prev.get("date", "?") + ")")
        for n, r in sets.items():
            pa = (prev.get("sets", {}).get(n, {}) or {}).get("accuracy", {})
            deltas = []
            for f in FACETS:
                now, was = r["accuracy"][f], pa.get(f)
                if now is not None and was is not None and abs(now - was) >= 0.005:
                    deltas.append(f"{f} {100*was:.0f}%→{100*now:.0f}%")
            L.append(f"- **{n}**: " + (", ".join(deltas) if deltas else "no change ≥0.5pt"))
        L.append("")

    report = "\n".join(L)
    open(a.out, "w", encoding="utf-8").write(report)
    print(report)
    # non-zero exit if the FROZEN set regressed on any always-graded facet (rolling is measurement-only)
    fr = sets.get("frozen-regression")
    if fr and any((fr["accuracy"][f] is not None and fr["accuracy"][f] < 1.0)
                  for f in ("stage", "source_type", "evidence_type", "strength")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
