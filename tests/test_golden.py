#!/usr/bin/env python3
"""
Golden regression tests — freeze the deterministic engine against silent rule drift.

The whole product rests on one promise: identical inputs always produce identical
output, so history stays comparable over time. These tests run the real ranking,
deduplication, classification and history functions over a fixed fixture set and
assert byte-for-byte on the results. If a rule changes (a ranking weight, a topic
predicate, a taxonomy label), a test here fails — forcing a conscious re-freeze of
the baseline below and a bump of TAXONOMY_VERSION, rather than a quiet reclassification
of the past.

Run:  python tests/test_golden.py          (standalone; exit 1 on failure)
  or: pytest tests/test_golden.py

No network, no packages: external deps are stubbed, and only pure functions are called.
Fixture dates are far in the past so the recency bonus is zero — ranking is therefore
date-independent and these tests give the same result on any day.
"""
import sys
import types
import importlib.util
from pathlib import Path

# --- import build.py with external deps stubbed (pure functions only) ---------
for _name in ("feedparser", "requests", "yaml"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
_bs4 = types.ModuleType("bs4")
_bs4.BeautifulSoup = object
sys.modules.setdefault("bs4", _bs4)

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build", _ROOT / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

# --- frozen fixtures ----------------------------------------------------------
# One representative item per lifecycle stage, plus a near-duplicate pair (g6/g7).
# Dates are far past → no recency component → deterministic ranking on any day.
_D = "2020-01-01"
FIXTURES = [
    {"id": "g1", "source": "FDA — AI device authorisations", "layer": "regulation",
     "url": "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K990001",
     "title": "FDA 510(k) cleared: OncoDetect AI for tumour imaging",
     "summary": "Applicant · Acme Health Inc", "tier": "monthly", "date": _D},
    {"id": "g2", "source": "CMS — coverage & payment notices", "layer": "access",
     "url": "https://www.federalregister.gov/documents/2026/01/01/cms-ntap",
     "title": "Medicare NTAP add-on payment for AI cardiology tool",
     "summary": "coverage decision; CPT coding update", "tier": "daily", "date": _D},
    {"id": "g3", "source": "ClinicalTrials.gov", "layer": "clinical",
     "url": "https://clinicaltrials.gov/study/NCT00000001",
     "title": "AI triage tool randomised trial",
     "summary": "Sponsor · Phase 3 · primary endpoint: cost-effectiveness and length of stay",
     "tier": "weekly", "date": _D},
    {"id": "g4", "source": "PubMed — AI health economics", "layer": "heor",
     "url": "https://pubmed.ncbi.nlm.nih.gov/00000001/",
     "title": "Cost-effectiveness of AI screening",
     "summary": "health technology assessment; systematic review", "tier": "weekly", "date": _D},
    {"id": "g5", "source": "arXiv (cs.AI)", "layer": "research",
     "url": "https://arxiv.org/abs/2401.00001",
     "title": "A foundation model for medical imaging",
     "summary": "large language model for radiology", "tier": "daily", "date": _D},
    {"id": "g6", "source": "STAT News", "layer": "industry",
     "url": "https://www.statnews.com/2026/01/01/tempus-raise",
     "title": "Tempus AI raises $200M for oncology diagnostics",
     "summary": "funding round", "tier": "daily", "date": _D},
    {"id": "g7", "source": "Endpoints News", "layer": "industry",
     "url": "https://endpts.com/tempus-raise-platform",
     "title": "Tempus AI raises $200M for oncology diagnostics platform",
     "summary": "series funding round", "tier": "daily", "date": _D},
]


def _clone():
    return [dict(i) for i in FIXTURES]


def _pipeline():
    """Collapse → QA gate → topic-tag, as in the real build. Returns (items, o)."""
    items = build.collapse_near_duplicates(_clone())
    items = build.validate_or_abort(items)
    build.tag_topics(items)
    return items, build.overview_stats(items)


# --- golden baselines (regenerate consciously if a rule intentionally changes) -
GOLDEN_SCORES = {"g1": 10, "g2": 7, "g3": 5, "g4": 2, "g5": 1, "g6": 1, "g7": 1}
GOLDEN_COLLAPSE_IDS = ["g1", "g2", "g3", "g4", "g5", "g7"]  # g6 merged into g7
GOLDEN_KEPT_DUP_TITLE = "Tempus AI raises $200M for oncology diagnostics platform"
GOLDEN_RANK_ORDER = ["g1", "g2", "g3", "g4", "g5", "g7"]
GOLDEN_TOP_STORY = ("Device authorisations", "g1")
GOLDEN_TOPICS = {
    "g1": ["fda-ai-authorisations", "regulatory-activity", "oncology-ai", "radiology-imaging-ai"],
    "g2": ["cms-coverage", "ntap-activity", "cpt-coding", "reimbursement-coverage", "cardiology-ai"],
    "g3": ["ai-clinical-studies", "economic-endpoint-trials"],
    "g4": ["hta-value-evidence"],
    "g5": ["ai-research", "radiology-imaging-ai"],
    "g7": ["oncology-ai"],
}
GOLDEN_LAYERS = {"research": 1, "clinical": 1, "regulation": 1, "heor": 1, "access": 1, "industry": 1}
GOLDEN_REGIONS = {"North America": 2}
GOLDEN_CLINICAL = {"Radiology & imaging": 2, "Oncology": 2, "Cardiology": 1}
GOLDEN_BODIES = {"FDA": 1, "CMS": 1}


# --- tests --------------------------------------------------------------------
def test_rank_scores():
    """Ranking weights are frozen — a changed weight shifts a score here."""
    for i in _clone():
        s, _ = build.rank_score(i)
        assert s == GOLDEN_SCORES[i["id"]], f"{i['id']} score {s} != {GOLDEN_SCORES[i['id']]}"


def test_near_duplicate_collapse():
    """The same story from two outlets collapses to one; the fuller title is kept."""
    collapsed = build.collapse_near_duplicates(_clone())
    assert [i["id"] for i in collapsed] == GOLDEN_COLLAPSE_IDS
    kept = [i["title"] for i in collapsed if i["id"] == "g7"]
    assert kept == [GOLDEN_KEPT_DUP_TITLE]


def test_ranking_order():
    """Overall ordering by score is stable and date-independent."""
    items, _ = _pipeline()
    order = [i["id"] for i in sorted(items, key=lambda i: -build.rank_score(i)[0])]
    assert order == GOLDEN_RANK_ORDER, f"{order} != {GOLDEN_RANK_ORDER}"


def test_top_story():
    """The featured story is selected by rule (device authorisation first)."""
    _, o = _pipeline()
    why, item = build._digest(o)[0]
    assert (why, item["id"]) == GOLDEN_TOP_STORY


def test_topic_tagging():
    """Every Follow-topic predicate assigns the same slugs to the same items."""
    items, _ = _pipeline()
    got = {i["id"]: i.get("topics", []) for i in items}
    for iid, slugs in GOLDEN_TOPICS.items():
        assert got[iid] == slugs, f"{iid}: {got[iid]} != {slugs}"


def test_stage_and_breakdown_counts():
    """Stage, region, clinical-area and body counts partition the build as frozen."""
    items, o = _pipeline()
    assert o["layers"] == GOLDEN_LAYERS
    assert dict(o["macro"]) == GOLDEN_REGIONS
    assert dict(o["focus"]) == GOLDEN_CLINICAL
    bodies = {n: c for role in ("regulator", "payer") for n, c in o["bodies"].get(role, [])}
    assert bodies == GOLDEN_BODIES


def test_export_schema():
    """The CSV/JSON export contract is frozen: fixed columns, self-describing JSON,
    and 'date unknown' preserved as empty (never guessed) — downstream consumers rely on it."""
    import tempfile, json, csv, io
    from pathlib import Path
    cols = ["id", "title", "url", "source", "source_type", "stage",
            "region", "country", "date", "score", "topics"]
    items = [
        {"id": "e1", "title": "FDA cleared AI", "url": "https://accessdata.fda.gov/K1",
         "source": "FDA — AI device authorisations", "layer": "regulation", "date": "2026-07-20",
         "topics": ["fda-ai-authorisations", "oncology-ai"], "score": 10,
         "region": "North America", "country": "United States", "stype": "Regulator"},
        {"id": "e2", "title": "NICE recommendation", "url": "https://www.nice.org.uk/n1",
         "source": "NICE — News", "layer": "access", "date": "",  # date unknown
         "topics": ["nice-evaluations"], "score": 6, "region": "Europe",
         "country": "United Kingdom", "stype": "HTA / payer"},
    ]
    tmp = Path(tempfile.mkdtemp())
    orig_docs = build.DOCS
    try:
        build.DOCS = tmp
        build.write_export(items)
        payload = json.loads((tmp / "data" / "feed-latest.json").read_text())
        assert payload["fields"] == cols
        assert payload["count"] == 2 and payload["taxonomy_version"] == build.TAXONOMY_VERSION
        assert payload["items"][1]["date"] == ""          # unknown date preserved, not guessed
        assert payload["items"][0]["topics"] == "fda-ai-authorisations;oncology-ai"
        rows = list(csv.DictReader(
            (tmp / "data" / "feed-latest.csv").read_text(encoding="utf-8-sig").splitlines()))
        assert list(rows[0].keys()) == cols and len(rows) == 2
    finally:
        build.DOCS = orig_docs


def test_term_counting_word_boundary():
    """Trend terms count with a leading word boundary (taxonomy 1.2): 'agent' counts
    agent/agents/agentic but NOT 'reagent'; embedded substrings don't inflate the count."""
    build.private_put = lambda *a, **k: True
    items = [
        {"id": "t1", "source": "arXiv", "layer": "research", "url": "https://arxiv.org/abs/1",
         "title": "An agentic AI agent coordinates multiple agents", "summary": ""},
        {"id": "t2", "source": "PubMed — AI × HTA/HEOR", "layer": "clinical", "url": "https://pubmed.ncbi.nlm.nih.gov/2/",
         "title": "Diagnostic reagent assay", "summary": "reagents prepared"},
    ]
    build.validate_or_abort(items)
    row, _ = build.log_history(items, ["agent", "bias"], token=None,
                              health={"contributing": 2, "expected": 2, "zero_steady": [], "failed": [], "undated": 0})
    assert row["terms"]["agent"] == 3, f"agent counted {row['terms']['agent']} (want 3: agentic/agent/agents, not reagent)"
    assert row["terms"]["bias"] == 0, f"bias counted {row['terms']['bias']} (no 'bias' word present)"


def test_geo_and_body_classification():
    """Freeze the geo + body rules added with the source expansion (taxonomy 1.1):
    LATAM resolves to Latin America, new European bodies map correctly, and the NoMA
    word-boundary guard doesn't misfire on oncology terms like 'melanoma'."""
    cases = [
        # source, layer, title, expected country, expected region, expected source_type
        ("LATAM — device authorisation (ANVISA / COFEPRIS)", "regulation",
         "ANVISA approves AI ECG device in Brazil", "Brazil", "Latin America", "Regulator"),
        ("LATAM — HTA & coverage (CONITEC)", "access",
         "CONITEC recommends coverage of AI screening", "Brazil", "Latin America", "HTA / payer"),
        ("LATAM — device authorisation (ANVISA / COFEPRIS)", "regulation",
         "COFEPRIS authorises AI software in Mexico", "Mexico", "Latin America", "Regulator"),
        ("Netherlands — Zorginstituut", "access",
         "Zorginstituut assesses AI diagnostic", "Netherlands", "Europe", "HTA / payer"),
    ]
    for src, layer, title, country, region, stype in cases:
        i = {"source": src, "layer": layer, "title": title, "summary": "", "url": "https://news.google.com/x"}
        assert build.country_of(i) == country, f"{title}: country {build.country_of(i)} != {country}"
        assert build.MACRO.get(country) == region, f"{country} region != {region}"
        assert build.source_type(i) == stype, f"{title}: source_type {build.source_type(i)} != {stype}"
    # NoMA (Norway) must not be tagged from oncology text
    mel = {"source": "AI/ML intervention trials", "layer": "clinical",
           "title": "AI for melanoma detection", "summary": "oncology", "url": "https://clinicaltrials.gov/x"}
    assert build.country_of(mel) is None
    bc = build._body_role_counts([mel])
    assert all(name != "NoMA" for role in bc.values() for name, _ in role)


def test_ai_relevance_filter():
    """Native feeds are gated for AI/digital-health relevance: real AI/digital items pass;
    clearly non-AI items (appointments, drug approvals, epidemiology, generic notices) drop."""
    keep = [
        "MHRA calls for regulation of AI in healthcare",
        "Deep-learning model detects atrial fibrillation on ECG",
        "FDA clears AI-enabled triage software",
        "DiGA listing for a prescription digital therapeutic",
        "Machine learning predicts hospital readmission",
        "Autonomous computer-aided detection cleared for colonoscopy",
        "MHRA clarifies regulatory status of ambient voice technologies in the NHS",
        "CMS proposes payment frameworks for software as a medical service",
        "Digital twins for chronic disease management",
    ]
    drop = [
        "EMA appoints new Deputy Executive Director and Head of Veterinary Medicines",
        "Thousands of patients to benefit from new daily pill for heart condition",
        "Global economic burden of depression in 154 countries",
        "Field Safety Notices: 29 June to 03 July 2026",
        "New leadership team appointments",
        "New lung preservation machine could make donor lungs available",
    ]
    for t in keep:
        assert build._ai_relevant(t), f"should KEEP: {t}"
    for t in drop:
        assert not build._ai_relevant(t), f"should DROP: {t}"


def test_relevance_gate():
    """The global gate keeps inherently-AI sources even without a keyword (device names,
    journal titles), and drops non-AI items from broad, non-AI-scoped sources. Note:
    'AI/ML intervention trials' is NO LONGER exempt — it must carry an AI keyword."""
    items = [
        {"source": "FDA — AI device authorisations", "layer": "regulation", "title": "OmniScan 3000", "summary": ""},
        {"source": "NEJM AI", "layer": "clinical", "title": "Detecting sepsis earlier", "summary": ""},
        {"source": "arXiv", "layer": "research", "title": "Scaling transformers", "summary": ""},
        {"source": "AI/ML intervention trials", "layer": "clinical", "title": "AI triage RCT for chest pain", "summary": ""},
        {"source": "PubMed — AI × HTA/HEOR", "layer": "heor", "title": "Predictors of atrial fibrillation recurrence after ablation", "summary": ""},
        {"source": "CMS — coverage & payment notices", "layer": "access",
         "title": "Medicare Program; Prospective Payment System for Skilled Nursing Facilities", "summary": ""},
        {"source": "Value in Health", "layer": "heor", "title": "Cost-effectiveness of statins", "summary": ""},
        {"source": "Value in Health", "layer": "heor", "title": "Cost-effectiveness of an AI triage tool", "summary": ""},
    ]
    kept = {i["title"] for i in build.relevance_gate(items)}
    assert {"OmniScan 3000", "Detecting sepsis earlier", "Scaling transformers",
            "AI triage RCT for chest pain", "Predictors of atrial fibrillation recurrence after ablation",
            "Cost-effectiveness of an AI triage tool"} <= kept
    assert "Medicare Program; Prospective Payment System for Skilled Nursing Facilities" not in kept
    assert "Cost-effectiveness of statins" not in kept


def test_health_gate_gnews():
    """Google-News items must be healthcare-relevant; native/journal items are NOT health-gated
    (so condition-only titles aren't lost). AI-native sources always pass."""
    items = [
        {"source": "Additional European HTA", "layer": "access", "gnews": True,
         "title": "Noma Labs Discovers Vulnerability in Open Source AI Agent Platform Ruflo", "summary": ""},
        {"source": "LATAM — HTA & coverage (CONITEC)", "layer": "access", "gnews": True,
         "title": "Avocados From Mexico Debuts Avo.AI Answer Engine", "summary": ""},
        {"source": "MHRA (UK)", "layer": "regulation", "gnews": True,
         "title": "MHRA calls for regulation of AI in healthcare", "summary": ""},
        {"source": "Nature Medicine", "layer": "clinical",   # native RSS, condition-only title, not gnews
         "title": "Deep-learning model detects atrial fibrillation", "summary": ""},
    ]
    kept = {i["title"] for i in build.relevance_gate(items)}
    assert "MHRA calls for regulation of AI in healthcare" in kept        # gnews + health
    assert "Deep-learning model detects atrial fibrillation" in kept      # native, not health-gated
    assert "Noma Labs Discovers Vulnerability in Open Source AI Agent Platform Ruflo" not in kept
    assert "Avocados From Mexico Debuts Avo.AI Answer Engine" not in kept


def test_reimbursement_precision():
    """Access items: keep genuine coverage/payer; company news → industry; else → regulation;
    private-insurer AI adoption is not a coverage decision."""
    items = [
        {"layer": "access", "title": "CMS proposes payment framework for software as a medical service", "summary": ""},
        {"layer": "access", "title": "AI use in health system must be deemed safe - HIQA", "summary": ""},
        {"layer": "access", "title": "DIAGNOS gets Health Canada licence for AI retinal analysis", "summary": ""},
        {"layer": "access", "title": "Luminopia partners with Spin Master on amblyopia digital therapeutic", "summary": ""},
        {"layer": "access", "title": "Mexico's GNP Seguros to leverage Palantir AI to strengthen insurance coverage", "summary": ""},
    ]
    build.refine_access_layer(items)
    by = {i["title"][:12]: i["layer"] for i in items}
    assert by["CMS proposes"] == "access"        # payment signal
    assert by["AI use in he"] == "access"         # HIQA payer/HTA body
    assert by["DIAGNOS gets"] == "regulation"     # licence, no coverage/company signal
    assert by["Luminopia pa"] == "industry"       # company partnership news
    assert by["Mexico's GNP"] == "industry"       # private insurer, no public-payer signal


def test_heor_precision():
    """HEOR keeps value/economic/HTA/RWE evidence; non-economic AI reviews → clinical; HEOR bodies stay."""
    items = [
        {"layer": "heor", "source": "PubMed — AI × HTA/HEOR",
         "title": "AI in determination of the postmortem interval: systematic review and meta-analysis", "summary": ""},
        {"layer": "heor", "source": "AI in HTA & market access",
         "title": "AI will likely grow the HTA industrial complex", "summary": ""},
        {"layer": "heor", "source": "Value in Health",
         "title": "Retraction notice to Integrating Generative AI Into Evidence Synthesis", "summary": ""},
    ]
    build.refine_heor_layer(items)
    lay = {i["title"][:10]: i["layer"] for i in items}
    assert lay["AI in dete"] == "clinical"   # no economic signal → reclassified
    assert lay["AI will li"] == "heor"       # HTA signal → stays
    assert lay["Retraction"] == "heor"       # HEOR-body source → always kept


def test_ctgov_ai_gate_and_pr_junk():
    """'AI/ML intervention trials' is now AI-gated (drops non-AI drug trials); 'Digital therapeutic
    & device trials' stays exempt; market-research PR is dropped; plain Google-News URLs are health-gated."""
    items = [
        {"source": "AI/ML intervention trials", "layer": "clinical", "url": "https://clinicaltrials.gov/study/N1",
         "title": "Testing the anti-cancer drug Glofitamab in mantle cell lymphoma", "summary": "Phase 2"},
        {"source": "AI/ML intervention trials", "layer": "clinical", "url": "https://clinicaltrials.gov/study/N2",
         "title": "Benchmarking large language models against tumour boards", "summary": "Observational"},
        {"source": "Digital therapeutic & device trials", "layer": "clinical", "url": "https://clinicaltrials.gov/study/N3",
         "title": "Providing an Optimized and Empowered Pregnancy (POPPY) randomized trial", "summary": "RCT"},
        {"source": "MEA regulation", "layer": "regulation", "gnews": True, "url": "https://news.google.com/x",
         "title": "Digital Health Market Size to Worth USD 1171 Billion by 2035", "summary": ""},
        {"source": "AI policy & guidance", "layer": "regulation", "url": "https://news.google.com/y",
         "title": "Meta signs EU AI Act transparency code amid deepfake surge", "summary": ""},
    ]
    kept = {i["title"][:12] for i in build.relevance_gate(items)}
    assert "Benchmarking" in kept       # LLM trial passes AI gate
    assert "Providing an" in kept       # DTx source exempt, no keyword needed
    assert "Testing the " not in kept   # non-AI drug trial dropped
    assert "Digital Heal" not in kept   # market-research PR dropped
    assert "Meta signs E" not in kept   # plain Google-News URL, not healthcare


def test_source_lookback():
    """Lookback derives from cadence — monthly journals get a wider window — unless overridden."""
    assert build._source_days({"tier": "daily"}, 10) == 10
    assert build._source_days({"tier": "weekly"}, 10) == 21
    assert build._source_days({"tier": "monthly"}, 10) == 45
    assert build._source_days({"tier": "monthly", "lookback": 60}, 10) == 60   # explicit wins
    assert build._source_days({"tier": "daily"}, 30) == 30                     # default respected


def test_source_caps_after_gate():
    """Per-source cap keeps the newest N for capped (whole-feed) sources; others pass untouched."""
    items = [{"source": "Nature Medicine", "title": f"p{i}", "date": f"2026-07-{10 + i:02d}"} for i in range(9)]
    items += [{"source": "AI/ML intervention trials", "title": f"t{i}", "date": "2026-07-20"} for i in range(9)]
    out = build.apply_source_caps(items, {"Nature Medicine": 6})   # ctgov source not capped
    nm = {i["title"] for i in out if i["source"] == "Nature Medicine"}
    ct = [i for i in out if i["source"] == "AI/ML intervention trials"]
    assert nm == {"p8", "p7", "p6", "p5", "p4", "p3"}   # newest 6 kept
    assert len(ct) == 9                                  # uncapped source untouched


def test_overtime_section():
    """Phase-2 Over-time analytics: guarded below the minimum build count; above it,
    renders both charts (Market activity + Evidence journey) with all six stage colours
    and a reconciled commercial-stage share. Deterministic, no <script>."""
    few = [{"date": f"2026-07-0{i}", "total": 3,
            "layers": {"research": 1, "clinical": 1, "regulation": 1, "heor": 0, "access": 0, "industry": 0}}
           for i in range(1, 3)]
    out_few = build.overtime_html(few)
    assert "Over time" in out_few and "unlock" in out_few.lower()
    assert "<svg" not in out_few          # no chart rendered below the guard

    many = [{"date": f"2026-07-{d:02d}", "total": 6,
             "layers": {"research": 1, "clinical": 1, "regulation": 1, "heor": 1, "access": 1, "industry": 1}}
            for d in range(1, 7)]
    out = build.overtime_html(many)
    assert "Daily evidence volume" in out and "Evidence journey" in out
    assert out.count("<svg") == 2
    assert "<script" not in out
    for hexcol in build.STAGE_COLOR.values():
        assert hexcol in out, f"missing stage colour {hexcol}"
    assert "33%" in out                    # commercial share = (regulation+access)/total = 2/6


def test_history_row_shape():
    """The daily history row carries every Phase-2 dimension, matching the site numbers."""
    build.private_put = lambda *a, **k: True   # no local/remote write during tests
    items, o = _pipeline()
    row, _ = build.log_history(
        items, ["NTAP", "EU AI Act", "foundation model", "cost-effectiveness"],
        token=None,
        health={"contributing": 6, "expected": 8, "zero_steady": [], "failed": [], "undated": 0},
        o=o,
    )
    assert row["layers"] == GOLDEN_LAYERS
    assert row["regions"] == GOLDEN_REGIONS
    assert row["clinical"] == GOLDEN_CLINICAL
    assert row["bodies"] == GOLDEN_BODIES
    # topic column set is the full registry (stable schema, zeros kept)
    assert set(row["topics"]) == {t["slug"] for t in build.TOPICS}
    assert row["topics"]["oncology-ai"] == 2 and row["topics"]["ema-activity"] == 0
    # QA outcome recorded and passing
    assert row["qa"]["published"] == 6 and row["qa"]["dropped"] == 0 and row["qa"]["passed"] is True


# --- standalone runner --------------------------------------------------------
def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} golden tests passed"
          f" (taxonomy v{build.TAXONOMY_VERSION})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
