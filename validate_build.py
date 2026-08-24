"""Post-build validation harness — a regression ORACLE for the AI-in-Health Evidence Monitor.

Runs AFTER build.py has classified, ranked and RENDERED a build. The guiding principle: never validate
the page against itself. For every important number or classification, the expected value is derived
INDEPENDENTLY from the final canonical item set, then compared with what the build computed and with
what the page actually rendered.

Deterministic (no LLM), non-blocking (the caller always publishes), defensive (a broken check can never
abort the build). Two severities: ERROR (near-certain defect) and WARN (soft fit / display concern).

Layered, in dependency order — later layers are skipped if item integrity has already failed, because
their results would be misleading noise:

  1. E — Evidence / item integrity        (schema, ids, urls, dates, routes)
  2. S — Scope / classification integrity  (drug/vet/materials leaks, commentary, research fit)
  3. F — Facet integrity                   (every facet value is actually filterable)
  4. H — Home invariants                   (featured story, metric tiles, ranking)
  5. A — Analysis invariants               (counts, gates, denominators, reconciliations)
  6. R — Render / source reconciliation    (the rendered page exposes the canonical values)
  7. Z — Empty-state invariants            (empty states appear iff a section is genuinely empty)
  8. X — Cross-page invariants             (a tag never drifts from its rule)
"""

from __future__ import annotations
import html as _html
import json
import re
from datetime import datetime, timezone, timedelta

WARN_EMAIL_THRESHOLD = 4        # email fires on ANY error, or on this many warnings
_CLOCK_SKEW_DAYS = 1            # a date within this many days ahead of the build is not "future"
_LINK_SAMPLE = 8               # external URLs sampled for reachability per build (keeps the build fast)
_GNEWS_MAX_PCT = 15            # warn if more than this % of published items still use unresolved google URLs

# Item-integrity ERROR codes — if any of these fire, the aggregate layers (H/A/R/Z/X) are skipped.
_INTEGRITY_CODES = {"E01_missing_field", "E02_bad_url", "E03_duplicate_id", "E04_duplicate_url",
                    "E05_future_date", "E06_bad_stage", "E08_filename_title"}

_KNOWN_ETYPES = {
    "AI governance", "Acquisition", "Budget impact", "Commentary", "Company strategy",
    "Consultation / policy", "Deployment", "Economic evaluation", "Enforcement / safety",
    "Executive move", "Funding round", "HEOR / value", "HTA perspective", "HTA report",
    "Industry analysis", "Industry news", "Journal study", "Legal / litigation", "Market access",
    "Market signal", "Meta-analysis", "Methodology", "News", "Partnership", "Payment / coverage",
    "Policy signal", "Preprint", "Primary evidence", "Product launch", "RCT", "Real-world evidence",
    "Regulatory authorisation", "Regulatory guidance", "Regulatory programme", "Review",
    "Rule / legislation", "Secondary evidence", "Study protocol", "Systematic review",
    "Trial registry", "Value framework",
}
_SPECIALTY_TOPIC = {
    "Oncology": "oncology-ai", "Cardiology": "cardiology-ai",
    "Radiology & imaging": "radiology-imaging-ai", "Mental health": "mental-health-ai",
}
# Stage ↔ evidence-type coherence (audit finding, 2026-08-12): News/VC/commercial items must not sit in
# an evidence stage; studies must not sit in commercial/regulatory/access stages; regulatory/coverage
# types must be in their own stage. Each rule = (etype-set, forbidden-stage-set, code, message).
_EVIDENCE_STAGES = {"clinical", "research", "heor"}
_COHERENCE_RULES = (
    ({"News", "Industry news", "Industry analysis", "Funding round", "Product launch", "Partnership",
      "Executive move", "Acquisition", "Company strategy"}, _EVIDENCE_STAGES,
     "C01_news_in_evidence", "News/commercial item in an evidence stage"),
    ({"Trial registry", "RCT", "Journal study", "Systematic review", "Meta-analysis",
      "Real-world evidence", "Study protocol"}, {"industry", "regulation", "access"},
     "C02_study_outside_evidence", "Study/trial item outside an evidence stage"),
    ({"Regulatory authorisation", "Regulatory guidance", "Regulatory programme", "Rule / legislation",
      "Enforcement / safety"}, {"clinical", "research", "heor", "industry"},
     "C03_regulatory_misplaced", "Regulatory item outside the regulation stage"),
    ({"Payment / coverage"}, {"clinical", "research", "heor", "industry"},
     "C04_coverage_misplaced", "Coverage item outside the access stage"),
    ({"HEOR / value", "HTA perspective", "Value framework", "Budget impact"},
     {"clinical", "research", "industry", "regulation", "access"},
     "C05_heor_misplaced", "HEOR/value item outside the HEOR stage"),
)
_STRENGTH_VOCAB = {"Primary evidence", "Secondary evidence", "Policy signal", "Market signal", "Commentary"}
_RELEVANCE_VOCAB = {"Direct clinical", "Healthcare operations", "Biomedical research", "Adjacent AI", "General AI"}
_MODALITY_VOCAB = {"", "Imaging AI", "Generative AI / LLM", "Clinical decision support", "Digital therapeutic",
                   "Predictive ML", "Remote monitoring", "Robotics", "Drug discovery AI"}
_MATURITY_VOCAB = {"", "Discovery", "Retrospective", "Prospective", "Randomised", "Real-world", "Synthesis",
                   "Economic model", "HTA", "Value", "Value evidence", "Methodology"}


class Issue:
    __slots__ = ("level", "page", "code", "title", "detail")

    def __init__(self, level, page, code, title, detail=""):
        self.level, self.page, self.code, self.title, self.detail = level, page, code, title, detail


class Report:
    def __init__(self, meta):
        self.meta = meta
        self.issues: list[Issue] = []
        self.snapshot: dict = {}
        self.checks_run = 0
        self._skipped: set = set()          # canonical layer letters that did NOT run (≠ passed)
        self.layers_skipped: list[str] = []  # finalised, ordered, from _skipped at the end of the run

    def err(self, page, code, title, detail=""):
        self.issues.append(Issue("ERROR", page, code, title, detail))

    def warn(self, page, code, title, detail=""):
        self.issues.append(Issue("WARN", page, code, title, detail))

    @property
    def errors(self):
        return [i for i in self.issues if i.level == "ERROR"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.level == "WARN"]

    @property
    def email_trigger(self):
        return len(self.errors) > 0 or len(self.warnings) >= WARN_EMAIL_THRESHOLD

    def status_dict(self):
        return {
            "ok": len(self.issues) == 0,
            "email_trigger": self.email_trigger,
            "n_errors": len(self.errors),
            "n_warnings": len(self.warnings),
            "generated_at": self.meta.get("generated_at"),
            "taxonomy_version": self.meta.get("taxonomy_version"),
            "codes": sorted({i.code for i in self.issues}),
            "layers_skipped": self.layers_skipped,
        }

    def console_summary(self):
        sk = f" · skipped: {','.join(self.layers_skipped)}" if self.layers_skipped else ""
        return (f"validation: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
                f" — email={'YES' if self.email_trigger else 'no'}{sk}")

    def _snapshot_lines(self):
        s = self.snapshot
        if not s:
            return []
        st = s.get("stages", {})
        return [
            "## Build summary", "",
            f"- **Items:** {s.get('n_items','?')} "
            f"(Research {st.get('research',0)} · Clinical {st.get('clinical',0)} · "
            f"Regulatory {st.get('regulation',0)} · HEOR {st.get('heor',0)} · "
            f"Access {st.get('access',0)} · Industry {st.get('industry',0)})",
            f"- **Featured story:** {s.get('featured','—')}",
            f"- **Authorisation gate:** {s.get('authorisations',0)} · **Coverage gate:** {s.get('coverage',0)}",
            f"- **Trials:** {s.get('trials',0)} ({s.get('econ',0)} with an economic endpoint) · "
            f"**HTA & value:** {s.get('hta_value',0)}",
            f"- **Checks run:** {self.checks_run} layers"
            + (f" · skipped after integrity failure: {', '.join(self.layers_skipped)}" if self.layers_skipped else ""),
            "",
        ]

    def to_markdown(self):
        m = self.meta
        out = [f"# Build validation — {m.get('taxonomy_version','?')}",
               f"_🕒 Built {_fmt_ts(m.get('generated_at',''))} · {m.get('n_items','?')} items · "
               f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)_", ""]
        if self.meta.get("selftest_banner"):
            out.insert(2, f"> 🧪 **{self.meta['selftest_banner']}**\n")
        out += self._snapshot_lines()
        if not self.issues:
            out.append("✅ **All checks passed.** Home, Evidence and Analysis reconcile with the rendered "
                       "page; no scope leaks; empty states consistent; every tag matches its rule.")
            return "\n".join(out)
        for level, bucket in (("ERROR", self.errors), ("WARN", self.warnings)):
            if not bucket:
                continue
            out.append(f"## {level}s ({len(bucket)})\n")
            for i in bucket:
                out.append(f"- **[{i.page}] {i.title}**  `({i.code})`")
                if i.detail:
                    out.append(f"  \n  {i.detail}")
            out.append("")
        return "\n".join(out)

    def to_html(self):
        m = self.meta
        banner = ""
        if m.get("selftest_banner"):
            banner = (f'<div style="background:#fff4e5;border:1px solid #e0a300;border-radius:8px;'
                      f'padding:10px 14px;margin:0 0 12px;font-size:14px">🧪 <b>{_html.escape(m["selftest_banner"])}</b></div>')
        _err_color = "#137333" if not self.errors else "#b3261e"          # green at 0, red otherwise
        _warn_color = "#8a6d00" if self.warnings else "#137333"           # amber when present, else green
        head = (banner
                + f'<h2 style="margin:0 0 4px">Build validation — {_html.escape(str(m.get("taxonomy_version","?")))}</h2>'
                f'<p style="margin:0 0 12px;color:#555">🕒 Built {_html.escape(_fmt_ts(m.get("generated_at","")))} · '
                f'{m.get("n_items","?")} items · <b style="color:{_err_color}">{len(self.errors)} error(s)</b>, '
                f'<b style="color:{_warn_color}">{len(self.warnings)} warning(s)</b></p>')
        s = self.snapshot
        if s:
            st = s.get("stages", {})
            head += (f'<div style="background:#f7f7f7;border-radius:8px;padding:10px 14px;margin:0 0 14px;'
                     f'font-size:14px;line-height:1.6"><b>Build summary</b><br>'
                     f'Items: <b>{s.get("n_items","?")}</b> (Research {st.get("research",0)} · '
                     f'Clinical {st.get("clinical",0)} · Regulatory {st.get("regulation",0)} · '
                     f'HEOR {st.get("heor",0)} · Access {st.get("access",0)} · Industry {st.get("industry",0)})<br>'
                     f'Featured: <b>{_html.escape(str(s.get("featured","—")))}</b><br>'
                     f'Authorisation gate: <b>{s.get("authorisations",0)}</b> · '
                     f'Coverage gate: <b>{s.get("coverage",0)}</b> · Trials: {s.get("trials",0)} '
                     f'({s.get("econ",0)} econ) · HTA &amp; value: {s.get("hta_value",0)}</div>')
        if not self.issues:
            return head + '<p style="color:#137333"><b>✅ All checks passed.</b></p>'
        rows = []
        for i in self.issues:
            color = "#b3261e" if i.level == "ERROR" else "#8a6d00"
            rows.append(f'<tr><td style="padding:6px 10px;color:{color};font-weight:600">{i.level}</td>'
                        f'<td style="padding:6px 10px;white-space:nowrap">{_html.escape(i.page)}</td>'
                        f'<td style="padding:6px 10px"><b>{_html.escape(i.title)}</b>'
                        f'<div style="color:#555;font-size:13px">{_html.escape(i.detail)}</div></td>'
                        f'<td style="padding:6px 10px;color:#888;font-family:monospace;font-size:12px">'
                        f'{_html.escape(i.code)}</td></tr>')
        return head + ('<table style="border-collapse:collapse;font-family:system-ui,Arial;font-size:14px">'
                       '<thead><tr style="background:#f2f2f2">'
                       '<th style="padding:6px 10px;text-align:left">Level</th>'
                       '<th style="padding:6px 10px;text-align:left">Page</th>'
                       '<th style="padding:6px 10px;text-align:left">Issue</th>'
                       '<th style="padding:6px 10px;text-align:left">Code</th></tr></thead><tbody>'
                       + "".join(rows) + '</tbody></table>')


def _fmt_ts(ts):
    """'2026-08-11T20:01:12Z' -> '2026-08-11 20:01 UTC' for human-readable headers."""
    ts = str(ts or "")
    if "T" in ts:
        d, t = ts.split("T", 1)
        return f"{d} {t[:5]} UTC"
    return ts


def _text(i):
    return i.get("title", "") + " " + i.get("summary", "")


def _lower(i):
    return _text(i).lower()


def _canon_url(u):
    """Canonical form for duplicate detection: drop fragment and trailing slash."""
    u = (u or "").split("#", 1)[0]
    return u[:-1] if u.endswith("/") and len(u) > 8 else u


def run_validation(items, o, health, meta, B, rendered_html=None):
    """items: final in-memory list · o: overview_stats(items) · health: diagnostics dict · meta: build
    metadata (build_ts, docs_dir, check_links) · B: the build module · rendered_html: the written
    docs/index.html (enables the R/Z render-reconciliation layers). Returns a Report."""
    R = Report(meta)
    build_ts = meta.get("build_ts") or datetime.now(timezone.utc)
    build_date = build_ts.date() if hasattr(build_ts, "date") else datetime.now(timezone.utc).date()
    id_set = {i.get("id") for i in items}

    # ---- snapshot for the per-build summary email (always populated) ----
    try:
        feat0 = B.select_featured(o)
        R.snapshot = {
            "n_items": len(items), "stages": dict(o.get("layers", {})),
            "featured": (f'{feat0[1].get("title","")[:70]} — {feat0[1].get("source","")}' if feat0 else "quiet day"),
            "authorisations": len(o.get("authorisations", [])), "coverage": len(o.get("coverage_actions", [])),
            "trials": len(o.get("trials", [])), "econ": len(o.get("econ", [])),
            "hta_value": len(o.get("papers", [])),
        }
    except Exception:
        R.snapshot = {"n_items": len(items)}

    def _safe(fn):
        try:
            fn()
        except Exception as e:
            R.warn("Validator", "V00_check_error", f"check {fn.__name__} raised {type(e).__name__}", str(e))

    # ================= LAYER E — item integrity =================
    def check_fields():
        seen_id, seen_url = set(), set()
        for i in items:
            miss = [k for k in ("id", "title", "url", "source", "layer") if not i.get(k)]
            if miss:
                R.err("Evidence", "E01_missing_field", f"Item missing {', '.join(miss)}",
                      (i.get("title") or i.get("url") or "?")[:90])
            u = i.get("url", "")
            if not (u.startswith("http://") or u.startswith("https://")):
                R.err("Evidence", "E02_bad_url", "Item has a non-http(s) or empty link",
                      f"{i.get('title','?')[:70]} → {u!r}")
            if i.get("id") in seen_id:
                R.err("Evidence", "E03_duplicate_id", "Duplicate canonical id in the final dataset", str(i.get("id")))
            seen_id.add(i.get("id"))
            cu = _canon_url(u)
            if cu in seen_url:
                R.err("Evidence", "E04_duplicate_url", "Duplicate canonical URL in the final dataset", cu[:90])
            seen_url.add(cu)
            if i.get("layer") not in B.LAYERS:
                R.err("Evidence", "E06_bad_stage", "Item has an unknown stage",
                      f"{i.get('title','?')[:60]} → {i.get('layer')!r}")
            et = i.get("etype")
            if et and et not in _KNOWN_ETYPES:
                R.warn("Evidence", "E07_unknown_etype", "Item has an out-of-vocabulary evidence type",
                       f"{i.get('title','?')[:60]} → {et!r}")
            if i.get("date"):
                d = B._pdate(i.get("date", ""))
                if d and d > build_date + timedelta(days=_CLOCK_SKEW_DAYS):
                    R.err("Evidence", "E05_future_date",
                          "Item date is beyond the build time + allowed clock skew",
                          f"{i.get('title','?')[:55]} → {i.get('date')} (build {build_date})")
            if B._FILENAME_TITLE_RE.search(i.get("title", "")):
                R.err("Evidence", "E08_filename_title", "Item title looks like an ingestion artefact",
                      i.get("title", "")[:80])
            # E11: a leftover " | Journal/Source" tag is a title-cleaning miss (should have been stripped)
            if " | " in i.get("title", ""):
                R.warn("Evidence", "E11_title_source_tag", "Title still carries a ' | source' tag",
                       i.get("title", "")[:80])

    def check_routes():
        # E10: internal assets the page links must exist on disk.
        docs = meta.get("docs_dir")
        if docs:
            import os
            need = ["feed.xml", "feed.xsl", "data/feed-latest.json", "data/feed-latest.csv"]
            need += [f"feed-{t['slug']}.xml" for t in B.TOPICS]
            for rel in need:
                if not os.path.exists(os.path.join(docs, rel)):
                    R.err("Evidence", "E10_internal_route", "A linked internal asset is missing", rel)
        # E09: sampled external-URL reachability (definitive 404/410 only; never fails on transient/timeout).
        if meta.get("check_links", True):
            try:
                import requests
                ext = [i for i in items if "http" in i.get("url", "")]
                sample = ext[:: max(1, len(ext) // _LINK_SAMPLE)][:_LINK_SAMPLE]
                for i in sample:
                    try:
                        r = requests.head(i["url"], timeout=8, allow_redirects=True)
                        if r.status_code in (404, 410):
                            R.warn("Evidence", "E09_dead_link", "Sampled source link returns 404/410",
                                   f"{i.get('title','?')[:55]} → {i['url'][:60]}")
                    except Exception:
                        pass   # transient/timeout/blocked HEAD is NOT a defect
            except Exception:
                pass
        # E12: feed-wide Google-News prevalence — many UNRESOLVED google redirect URLs means the gnews
        # resolver failed or coverage leans too hard on Google News (a provenance-quality signal the
        # single featured-story check H04 can't see). Non-blocking warn.
        gnews = [i for i in items if "news.google.com" in (i.get("url") or "")]
        if items and 100 * len(gnews) / len(items) > _GNEWS_MAX_PCT:
            R.warn("Evidence", "E12_gnews_prevalence",
                   f"{len(gnews)}/{len(items)} items ({100*len(gnews)//len(items)}%) still use unresolved "
                   f"Google-News redirect URLs (>{_GNEWS_MAX_PCT}%) — resolver down or over-reliant on Google News")

    # ================= LAYER S — scope integrity =================
    def check_scope():
        for i in items:
            blob, low = _text(i), _lower(i)
            if B._DRUG_APPROVAL_RE.search(blob) and not B._AI_DEVICE_SCOPE_RE.search(blob):
                R.err("Evidence", "S01_drug_approval_leak",
                      "Pharmaceutical drug approval slipped the relevance gate", i.get("title", "")[:90])
            if B._OUT_OF_SCOPE_RE.search(low):
                R.err("Evidence", "S02_out_of_scope_leak",
                      "Out-of-scope (vet/agri/materials) item present", i.get("title", "")[:90])
            try:
                if B.classify_evidence(i)[0] == "Commentary":
                    R.err("Evidence", "S03_commentary_present",
                          "Commentary item present (should be excluded)", i.get("title", "")[:90])
            except Exception:
                pass
            if i.get("layer") == "research":
                t, s = i.get("title", ""), i.get("summary", "")
                if "arxiv" in (i.get("url", "") + i.get("source", "")).lower():
                    if not B._BIOMED_TITLE_RE.search(t):
                        R.warn("Evidence", "S06_research_not_biomedical",
                               "arXiv research item has no biomedical term in its title", t[:90])
                elif not B._health_relevant(t, s):
                    R.warn("Evidence", "S06_research_not_health",
                           "Research-layer item is not clearly health-relevant", t[:90])

    # ================= LAYER F — facet integrity (functional: ERROR) =================
    def check_facets():
        for i in items:
            for field, vocab, code in (("strength", _STRENGTH_VOCAB, "F01_strength_vocab"),
                                       ("relevance", _RELEVANCE_VOCAB, "F02_relevance_vocab"),
                                       ("modality", _MODALITY_VOCAB, "F03_modality_vocab"),
                                       ("maturity_lab", _MATURITY_VOCAB, "F04_maturity_vocab")):
                v = i.get(field)
                if v is not None and v not in vocab:
                    R.err("Evidence", code, f"Item {field} is not a selectable filter option",
                          f"{i.get('title','?')[:55]} → {v!r}")

    # ================= LAYER H — home invariants =================
    def check_home():
        feat = B.select_featured(o)
        if feat:
            why, hi = feat
            if hi.get("id") not in id_set:
                R.err("Home", "H01_featured_absent", "Featured story is not in the feed", str(hi.get("id")))
            blob = _text(hi)
            if B._LITIGATION_RE.search(blob.lower()):
                R.err("Home", "H02_featured_litigation", "Featured story is a litigation item", hi.get("title", "")[:90])
            if B._REFERENCE_GUIDE_RE.search(hi.get("title", "")):
                R.err("Home", "H03_featured_reference_guide",
                      "Featured story is a reference-guide chapter, not a regulator action", hi.get("title", "")[:90])
            if B._DRUG_APPROVAL_RE.search(blob) and not B._AI_DEVICE_SCOPE_RE.search(blob):
                R.err("Home", "H03_featured_drug", "Featured story is a drug approval", hi.get("title", "")[:90])
            if "news.google.com" in hi.get("url", "") and any(
                    "news.google.com" not in it.get("url", "") for _, it in B._digest(o)):
                R.warn("Home", "H04_featured_gnews",
                       "Featured story uses a Google-News link while a primary source was available",
                       hi.get("title", "")[:90])
        exp_reg = sum(1 for i in items if i.get("layer") == "regulation")
        exp_clin = sum(1 for i in items if i.get("layer") == "clinical")
        if o["layers"].get("regulation", 0) != exp_reg:
            R.err("Home", "H05_regulatory_metric", "Regulatory-updates tile disagrees with the feed",
                  f"tile={o['layers'].get('regulation')} vs recomputed={exp_reg}")
        if o["layers"].get("clinical", 0) != exp_clin:
            R.err("Home", "H05_clinical_metric", "Clinical-studies tile disagrees with the feed",
                  f"tile={o['layers'].get('clinical')} vs recomputed={exp_clin}")
        ranked = sorted(items, key=lambda i: -(i.get("score") or 0))[:5]
        for i in ranked:
            blob = _text(i)
            if B._DRUG_APPROVAL_RE.search(blob) and not B._AI_DEVICE_SCOPE_RE.search(blob):
                R.err("Home", "H06_top_scope_leak", "A top-5 ranked item is a drug approval", i.get("title", "")[:90])
        scores = [(i.get("score") or 0) for i in ranked]
        if scores != sorted(scores, reverse=True):
            R.warn("Home", "H07_ranking_order", "Top-updates scores are not non-increasing", str(scores))

    # ================= LAYER A — analysis invariants =================
    def check_analysis():
        from collections import Counter
        n = len(items)
        if sum(o["layers"].values()) != n:
            R.err("Analysis", "A01_stage_sum", "Stage counts do not sum to the item total",
                  f"sum={sum(o['layers'].values())} vs items={n}")
        rc = Counter(r for r in (B.MACRO.get(B.country_of(i)) for i in items) if r)
        if any(v > n for v in rc.values()) or sum(rc.values()) > n:
            R.err("Analysis", "A02_region_bounds", "Region counts exceed the item total", str(dict(rc)))
        cc = Counter(c for c in (B.country_of(i) for i in items) if c)
        placed = sum(1 for i in items if B.country_of(i))
        if sum(cc.values()) != placed:
            R.err("Analysis", "A03_country_sum", "Country tally does not reconcile with placed items",
                  f"sum={sum(cc.values())} vs placed={placed}")
        # A13: an item with a country must also map to a region — otherwise it drops out of the 'By
        # region' breakdown, making the region total silently undercount vs 'By country'.
        no_region = {i.get("country") for i in items if i.get("country") and not i.get("region")}
        if no_region:
            R.warn("Analysis", "A13_country_no_region", "Country present but no region mapping",
                   f"{len(no_region)} unmapped: {sorted(no_region)[:6]}")
        # A14: a source contributing several items where EVERY one is undated is a systematic date-
        # extraction gap for that feed (e.g. a publisher whose RSS omits pubDate) — surface it.
        by_src = {}
        for i in items:
            by_src.setdefault(i.get("source", "?"), []).append(i)
        for src, group in by_src.items():
            if len(group) >= 3 and all(not i.get("date") for i in group):
                R.warn("Analysis", "A14_source_all_undated",
                       f"Every item from '{src}' is undated (systematic date gap)", f"{len(group)} items")
        gate_ids = {id(x) for x in o.get("authorisations", [])}
        for i in items:
            if B._is_device_authorisation(i) and id(i) not in gate_ids:
                R.err("Analysis", "A04_authorisation_undercount",
                      "A genuine device authorisation is missing from the Authorisation gate", i.get("title", "")[:90])
        fda_expected = sum(1 for i in items if B._is_fda_authorisation(i))
        fda_tagged = sum(1 for i in items if "fda-ai-authorisations" in (i.get("topics") or []))
        if fda_expected != fda_tagged:
            R.err("Analysis", "A05_fda_feed_mismatch", "FDA-authorisations feed disagrees with the FDA rule",
                  f"rule={fda_expected} vs tagged={fda_tagged}")
        for i in o.get("trials", []):
            if i.get("layer") != "clinical":
                R.err("Analysis", "A06_trial_denominator",
                      "A non-clinical item is counted in the 'N AI trials' denominator",
                      f"{i.get('title','?')[:70]} (stage={i.get('layer')})")
        tset = {id(x) for x in o.get("trials", [])}
        for i in o.get("econ", []):
            if id(i) not in tset:
                R.err("Analysis", "A07_econ_subset", "An economic-endpoint trial is not in the trial set",
                      i.get("title", "")[:80])
        if len(o.get("papers", [])) != o["layers"].get("heor", 0):
            R.err("Analysis", "A09_heor_mismatch", "HTA & value tile disagrees with the HEOR stage",
                  f"papers={len(o.get('papers', []))} vs heor={o['layers'].get('heor')}")
        scan = dict(B.clinical_focus(items))
        tcounts = B._topic_counts(items)
        for label, slug in _SPECIALTY_TOPIC.items():
            a, b = scan.get(label, 0), tcounts.get(slug, 0)
            if abs(a - b) >= 2:
                R.warn("Analysis", "A08_specialty_divergence",
                       f"'{label}' disagrees between the specialty tally and the {slug} follow-topic",
                       f"specialty-scan={a} vs follow-topic={b}")
        cov_expected = [i for i in items if i.get("layer") == "access"
                        and any(k in i.get("source", "") for k in ("CMS", "NICE", "Federal"))
                        and not B._LITIGATION_RE.search(_lower(i))]
        if len(cov_expected) != len(o.get("coverage_actions", [])):
            R.err("Analysis", "A11_coverage_gate", "Coverage gate disagrees with an independent recomputation",
                  f"recomputed={len(cov_expected)} vs rendered={len(o.get('coverage_actions', []))}")
        # A12: the intended rule is that each item contributes AT MOST ONCE to regulator attribution and
        # at most once to payer attribution. Verify directly: the sum of the per-body tally for a role
        # must equal the number of items independently attributed to that role — if any item were
        # double-counted across two bodies of the same role, the tally-sum would exceed it.
        try:
            bodies = B._body_role_counts(items)
            for role in ("regulator", "payer"):
                total = sum(c for _, c in bodies.get(role, []))
                attributed = 0
                for i in items:
                    src = i.get("source", "")
                    text = (src + " " + i.get("title", "") + " " + i.get("summary", "")).lower()
                    if any((b in src) or (b in B.SAFE_TEXT_BODIES
                                          and re.search(rf"\b{re.escape(b.lower())}\b", text))
                           for b, r in B.BODY_ROLE.items() if r == role):
                        attributed += 1
                if total != attributed:
                    R.err("Analysis", "A12_body_attribution",
                          f"{role} attribution is not one-per-item",
                          f"tally-sum={total} vs items-attributed={attributed}")
        except Exception:
            pass

    # ================= LAYER R — render / source reconciliation =================
    def check_render():
        h = rendered_html
        if not h:
            R._skipped.add("R")   # skipped ≠ passed: no rendered HTML available to reconcile against
            return
        norm = _html.unescape(h)
        n = len(items)
        # R01 — every rendered item-count equals the canonical feed size
        for pat, where in ((r"·\s*([\d,]+)\s*updates", "header"),
                           (r"View all\s*([\d,]+)\s*updates", "footer"),
                           (r"Evidence\s*<span class=\"tabcount\">\(([\d,]+)\)", "tab")):
            m = re.search(pat, h)
            if not m:
                R.warn("Render", "R00_parse", f"could not read the {where} item count", pat)
            elif int(m.group(1).replace(",", "")) != n:
                R.err("Render", "R01_item_count", f"Rendered {where} count ≠ feed size",
                      f"rendered={m.group(1)} vs items={n}")
        # R02 — embedded ITEMS ids are a subset of the dataset and count matches
        m = re.search(r"const ITEMS=(\[.*?\]);const TOPIC_LABELS=", h, re.S)
        if not m:
            R.warn("Render", "R00_parse", "could not locate embedded ITEMS array")
        else:
            try:
                embedded = json.loads(m.group(1))
            except json.JSONDecodeError as e:
                R.err("Render", "R02_embedded_parse", "Embedded ITEMS is not valid JSON", str(e)[:80])
                embedded = None
            # Shape guard FIRST — a malformed-but-parseable value (e.g. not a list of objects) must not
            # silently degrade into an empty array and masquerade as a count mismatch.
            if embedded is not None and (not isinstance(embedded, list)
                                         or not all(isinstance(e, dict) for e in embedded)):
                R.err("Render", "R02_embedded_shape", "Embedded ITEMS is not a list of objects",
                      f"type={type(embedded).__name__}")
                embedded = None
            if embedded is not None:
                eids = [e.get("id") for e in embedded]
                if len(embedded) != n:
                    R.err("Render", "R02_embedded_count", "Embedded ITEMS count ≠ feed size",
                          f"embedded={len(embedded)} vs items={n}")
                extra = set(eids) - id_set
                if extra:
                    R.err("Render", "R02_embedded_ids", "Embedded ITEMS contain ids absent from the dataset",
                          f"{len(extra)} unknown id(s), e.g. {list(extra)[:3]}")
                # R03 — featured + top-5 ids are actually present in the embedded/rendered payload
                feat = B.select_featured(o)
                if feat and feat[1].get("id") not in set(eids):
                    R.err("Render", "R03_featured_rendered", "Featured story id is not in the rendered payload",
                          str(feat[1].get("id")))
                for it in sorted(items, key=lambda i: -(i.get("score") or 0))[:5]:
                    if it.get("url") and _html.unescape(it["url"]) not in norm:
                        R.err("Render", "R03_top_rendered", "A top-5 ranked item URL is not rendered on the page",
                              it.get("title", "")[:70])
                # R04 — rendered facet values equal the canonical facet values in the data. The embedded
                # ITEMS are the raw internal dicts (json.dumps(items)), so the keys are the internal
                # 'strength' / 'modality', not the export names.
                for field in ("strength", "modality"):
                    data_vals = {i.get(field) for i in items if i.get(field)}
                    emb_vals = {e.get(field) for e in embedded if e.get(field)}
                    if data_vals != emb_vals:
                        R.err("Render", "R04_facet_values", f"Rendered {field} values ≠ dataset values",
                              f"data={sorted(map(str,data_vals))} vs rendered={sorted(map(str,emb_vals))}")
        # R05 — rendered Home metric tiles equal the recomputed numbers. The label is capitalised and
        # pluralised by value ("regulatory<br>update" -> "Regulatory<br>updates"), so match on the stable
        # capitalised STEM before <br> and ignore the singular/plural suffix.
        for stem, expect in (("Regulatory", o["layers"].get("regulation", 0)),
                             ("Coverage", len(o.get("coverage_actions", []))),
                             ("Clinical", o["layers"].get("clinical", 0))):
            m = re.search(r'brief-v[^"]*">(\d+)</div><div class="brief-l">' + stem + r'<br>', h)
            if not m:
                R.warn("Render", "R00_parse", f"could not read the '{stem}' metric tile")
            elif int(m.group(1)) != expect:
                R.err("Render", "R05_metric_tile", f"Rendered '{stem}' tile ≠ recomputed",
                      f"rendered={m.group(1)} vs expected={expect}")

    # ================= LAYER Z — empty-state invariants =================
    def check_empty_states():
        h = rendered_html
        if not h:
            R._skipped.add("Z")   # skipped ≠ passed: no rendered HTML to check empty-states against
            return
        quiet = "A quiet day" in h
        feat = B.select_featured(o)
        # Z01 — the 'quiet day' featured empty-state shows iff there is no featured story
        if feat and quiet:
            R.err("Empty-state", "Z01_false_empty", "'A quiet day' empty-state shown though a featured story exists",
                  feat[1].get("title", "")[:70])
        if not feat and not quiet:
            R.err("Empty-state", "Z02_missing_empty", "No featured story and no 'A quiet day' empty-state rendered")
        # Z02 — Home metric tiles render '0' (not a blank/omission) for genuinely empty sections. Match
        # the capitalised stem before <br> (label is pluralised by value; see R05).
        for stem, val in (("Regulatory", o["layers"].get("regulation", 0)),
                         ("Coverage", len(o.get("coverage_actions", []))),
                         ("Clinical", o["layers"].get("clinical", 0))):
            if val == 0 and not re.search(r'brief-v[^"]*">0</div><div class="brief-l">' + stem + r'<br>', h):
                R.warn("Empty-state", "Z02_zero_tile", f"'{stem}' is 0 but the tile does not clearly render 0")

    # ================= LAYER A (coherence) — stage ↔ evidence-type =================
    def check_coherence():
        for i in items:
            et = i.get("etype") or B.classify_evidence(i)[0]
            stg = i.get("layer")
            for etset, bad_stages, code, msg in _COHERENCE_RULES:
                if et in etset and stg in bad_stages:
                    R.warn("Analysis", code, msg, f"[{stg}] {et}: {i.get('title','')[:70]}")
                    break
            # type↔content guards for the two etypes tightened in the classifier (audit 2026-08-12)
            if et == "Executive move" and not B._EV_EXEC.search(i.get("title", "").lower().replace("-", " ")):
                R.warn("Evidence", "C06_exec_move_no_action",
                       "'Executive move' type without an appointment/departure signal", i.get("title", "")[:70])
            if et == "Preprint" and (i.get("stype") or "") != "Preprint / research":
                R.warn("Evidence", "C07_preprint_wrong_source",
                       "'Preprint' type on a non-preprint source", f"{i.get('title','')[:55]} <{i.get('stype')}>")

    # ================= LAYER X — cross-page invariants =================
    def check_topic_tags():
        for t in B.TOPICS:
            for i in items:
                tagged = t["slug"] in (i.get("topics") or [])
                try:
                    should = bool(t["pred"](i))
                except Exception:
                    continue
                if tagged != should:
                    R.err("Cross-page", "X01_topic_tag_drift", f"Follow-topic '{t['slug']}' tag disagrees with its rule",
                          f"{i.get('title','?')[:55]} tagged={tagged} rule={should}")
                    break

    # ---- layered execution with dependency short-circuit ----
    integrity = (check_fields, check_routes)
    scope_facets = (check_scope, check_facets)
    aggregates = (check_home, check_analysis, check_coherence, check_render, check_empty_states, check_topic_tags)

    for fn in integrity + scope_facets:
        _safe(fn)
    R.checks_run = 3
    if any(i.code in _INTEGRITY_CODES for i in R.errors):
        # skipped ≠ passed: item integrity failed, so downstream layers did NOT run
        R._skipped.update({"H", "A", "R", "Z", "X"})
    else:
        for fn in aggregates:
            _safe(fn)
        R.checks_run = 8
    # finalise the skipped-layer list in canonical order (H → A → R → Z → X)
    R.layers_skipped = [ly for ly in ("H", "A", "R", "Z", "X") if ly in R._skipped]
    return R


# Injectable synthetic defects for the opt-in self-test. Each mutates a COPY of the items and must
# produce EXACTLY its own code (asserted via a baseline→injected delta). Only integrity-class defects
# are offered: they short-circuit the aggregate layers, so the delta is a single clean code with no
# incidental downstream noise. The published dataset/feed/page are NEVER touched — this runs on a copy.
_SELFTEST_CODES = ("E04_duplicate_url", "E05_future_date", "E06_bad_stage")


def _inject(copy_items, code):
    if code == "E04_duplicate_url" and len(copy_items) > 1:
        copy_items[1] = dict(copy_items[1]); copy_items[1]["url"] = copy_items[0]["url"]
    elif code == "E05_future_date":
        copy_items[0] = dict(copy_items[0]); copy_items[0]["date"] = "2099-01-01"
    elif code == "E06_bad_stage":
        copy_items[0] = dict(copy_items[0]); copy_items[0]["layer"] = "__selftest_bad_stage__"


def run_selftest(items, o, health, meta, B, rendered_html=None, expected_code="E04_duplicate_url"):
    """Prove the alarm path end-to-end WITHOUT touching production. Runs validation on a clean copy
    (baseline) and on a copy with one synthetic defect injected, then asserts the injection adds
    EXACTLY `expected_code` and nothing else. Returns (injected_report, detected, delta, expected)."""
    if expected_code not in _SELFTEST_CODES:
        expected_code = "E04_duplicate_url"
    m = dict(meta, check_links=False)
    baseline = run_validation([dict(i) for i in items], o, health, dict(m), B, rendered_html)
    base_codes = set(baseline.status_dict()["codes"])

    copy_items = [dict(i) for i in items]
    _inject(copy_items, expected_code)
    o_copy = B.overview_stats(copy_items)
    injected = run_validation(copy_items, o_copy, health, dict(m), B, rendered_html)
    inj_codes = set(injected.status_dict()["codes"])

    delta = inj_codes - base_codes
    detected = (delta == {expected_code})
    injected.meta["selftest_banner"] = (
        f"SELF-TEST — injected {expected_code} into a validation-only copy; the published feed, page and "
        f"docs/validation.json are untouched. Injection added exactly the expected code: {detected}"
        + ("" if detected else f" (got delta {sorted(delta)})") + ".")
    return injected, detected, delta, expected_code
