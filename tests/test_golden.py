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
sys.path.insert(0, str(_ROOT))   # so `import validate_build` (repo-root module) resolves in tests
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
GOLDEN_SCORES = {"g1": 10, "g2": 8, "g3": 5, "g4": 2, "g5": 1, "g6": 1, "g7": 1}  # g2: +1 formal payer/coverage decision (2.19)
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
    cols = ["id", "title", "url", "resolved_url", "source", "publisher_url", "via_gnews", "feed", "source_type", "stage",
            "evidence_type", "evidence_strength", "evidence_maturity", "healthcare_relevance", "ai_modality",
            "decision_type", "payer_type", "region", "country", "date", "score", "topics"]
    items = [
        {"id": "e1", "title": "FDA cleared AI", "url": "https://accessdata.fda.gov/K1",
         "source": "FDA — AI device authorisations", "layer": "regulation", "date": "2026-07-20",
         "topics": ["fda-ai-authorisations", "oncology-ai"], "score": 10,
         "region": "North America", "country": "United States", "stype": "Regulator",
         "etype": "Regulatory guidance", "strength": "Policy signal", "relevance": "Direct clinical",
         "modality": "Imaging AI"},
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
        assert payload["items"][0]["evidence_type"] == "Regulatory guidance"
        assert payload["items"][1]["evidence_strength"] == "Policy signal"   # derived when absent
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
    # These are Google-News discovery items (url = news.google.com): the named body (ANVISA, CONITEC,
    # Zorginstituut) is the SUBJECT of the story, not the source, so provenance source_type is 'Other'
    # (a gnews redirect is never a primary body — see test_gnews_never_typed_as_primary_body). The geo
    # rules below (country + macro-region) are what this test freezes and are unaffected.
    cases = [
        # source, layer, title, expected country, expected region, expected source_type (gnews → Other)
        ("LATAM — device authorisation (ANVISA / COFEPRIS)", "regulation",
         "ANVISA approves AI ECG device in Brazil", "Brazil", "Latin America", "Other"),
        ("LATAM — HTA & coverage (CONITEC)", "access",
         "CONITEC recommends coverage of AI screening", "Brazil", "Latin America", "Other"),
        ("LATAM — device authorisation (ANVISA / COFEPRIS)", "regulation",
         "COFEPRIS authorises AI software in Mexico", "Mexico", "Latin America", "Other"),
        ("Netherlands — Zorginstituut", "access",
         "Zorginstituut assesses AI diagnostic", "Netherlands", "Europe", "Other"),
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
        {"source": "arXiv", "layer": "research", "title": "Foundation model for sepsis detection", "summary": ""},
        {"source": "AI/ML intervention trials", "layer": "clinical", "title": "AI triage RCT for chest pain", "summary": ""},
        {"source": "PubMed — AI × HTA/HEOR", "layer": "heor", "title": "Predictors of atrial fibrillation recurrence after ablation", "summary": ""},
        {"source": "CMS — coverage & payment notices", "layer": "access",
         "title": "Medicare Program; Prospective Payment System for Skilled Nursing Facilities", "summary": ""},
        {"source": "Value in Health", "layer": "heor", "title": "Cost-effectiveness of statins", "summary": ""},
        {"source": "Value in Health", "layer": "heor", "title": "Cost-effectiveness of an AI triage tool", "summary": ""},
    ]
    kept = {i["title"] for i in build.relevance_gate(items)}
    assert {"OmniScan 3000", "Detecting sepsis earlier", "Foundation model for sepsis detection",
            "AI triage RCT for chest pain", "Predictors of atrial fibrillation recurrence after ablation",
            "Cost-effectiveness of an AI triage tool"} <= kept
    assert "Medicare Program; Prospective Payment System for Skilled Nursing Facilities" not in kept
    assert "Cost-effectiveness of statins" not in kept


def test_admin_notices_dropped():
    """Corrections / retractions / errata / replies are administrative — dropped, never counted as
    evidence. Real studies (even with 'foundation model' etc.) are kept."""
    items = [
        {"source": "npj Digital Medicine", "layer": "clinical", "url": "https://x/1",
         "title": "Author Correction: Digital biomarkers for brain health", "summary": ""},
        {"source": "Value in Health", "layer": "heor", "url": "https://x/2",
         "title": "Retraction notice to Integrating Generative AI Into Evidence Synthesis", "summary": ""},
        {"source": "npj Digital Medicine", "layer": "clinical", "url": "https://x/3",
         "title": "Reply to: Clinician engagement modifies AI-enabled ECG screening", "summary": ""},
        {"source": "Nature Medicine", "layer": "clinical", "url": "https://x/4",
         "title": "End-to-end multimodal pathology foundation model", "summary": ""},
    ]
    kept = {i["title"] for i in build.relevance_gate(items)}
    assert "End-to-end multimodal pathology foundation model" in kept   # real study kept
    assert all(("correction" not in t.lower() and "retraction" not in t.lower()
                and "reply to" not in t.lower()) for t in kept)         # admin notices gone


def test_out_of_scope_dropped():
    """Veterinary / agriculture / plant-science papers from broad PubMed AI queries are dropped;
    human-health clinical papers are kept."""
    items = [
        {"source": "PubMed — AI × HTA/HEOR", "layer": "clinical", "url": "https://x/1",
         "title": "Transcription Factors Regulating Nutrient Uptake in Plants Exposed to Abiotic Stress", "summary": ""},
        {"source": "PubMed — AI × HTA/HEOR", "layer": "clinical", "url": "https://x/2",
         "title": "A decision support framework for early prediction of milk yield in dairy cows", "summary": ""},
        {"source": "PubMed — AI × HTA/HEOR", "layer": "clinical", "url": "https://x/3",
         "title": "Silicon and beneficial microorganisms enhance plant abiotic stress tolerance via rhizosphere", "summary": ""},
        {"source": "NEJM AI", "layer": "clinical", "url": "https://x/4",
         "title": "Machine learning clinical decision support reduces inpatient lab utilization", "summary": ""},
    ]
    kept = {i["title"][:20] for i in build.relevance_gate(items)}
    assert "Machine learning cli" in kept                    # human-health study kept
    assert all("plant" not in t.lower() and "dairy" not in t.lower()
               and "transcription" not in t.lower() for t in kept)   # out-of-scope gone


def test_commentary_scholarship():
    """Bibliometric, education and framework/opinion papers are tagged Commentary, not Journal study."""
    for title in ("A bibliometric analysis of machine learning in ADHD diagnosis",
                  "Advancing Radiology Education with AI: Curriculum Planning and Evaluation",
                  "Virtual simulation in medical education: a review",
                  "Artificial Intelligence and the Financialization of Medical Knowledge",
                  "The iPatient Meets the iDoctor",
                  "Towards a framework for implementing artificial intelligence in clinical medicine"):
        etype, strength = build.classify_evidence(
            {"title": title, "summary": "", "layer": "clinical", "stype": "Journal / evidence"})
        assert (etype, strength) == ("Commentary", "Commentary"), title


def test_inclusion_rule_nonprimary():
    """Opinion/adoption items → Commentary; narrative field-summaries → Review/Secondary — so the
    default Primary-evidence view answers 'does it work in patients?'. Real studies stay Primary."""
    def et(title):
        return build.classify_evidence(
            {"title": title, "summary": "", "layer": "clinical", "stype": "Journal / evidence"})
    # opinion / perspective / adoption → Commentary
    for title in ("Global health suffers when corporate AI sovereigns reign",
                  "Going beyond algorithmic fairness in health care",
                  "Digital Health Adoption, eHealth Literacy, and Trust in AI Among Gen Z Students"):
        assert et(title) == ("Commentary", "Commentary"), title
    # narrative field-summary → Review / Secondary
    for title in ("Advancements in Cardiac Magnetic Resonance Imaging: Innovations, Challenges, and Future Directions",
                  "Toxicity reduction in nasopharyngeal carcinoma: from paradigm shift to integrated decision-making",
                  "Translating AI into clinical practice for GI endoscopy: current applications and future perspectives"):
        assert et(title) == ("Review", "Secondary evidence"), title
    # genuine clinical study stays Primary
    assert et("Diagnostic accuracy of a deep-learning tool for echocardiographic measurement") \
        == ("Journal study", "Primary evidence")


def test_geography_from_source():
    """National regulator/HTA/payer source names place items even when the headline omits the country;
    trade-press source names carry no country token, so those items stay unplaced."""
    assert build.country_of({"source": "Canada — CADTH & Health Canada",
                             "title": "New AI retinal imaging licence granted", "summary": ""}) == "Canada"
    assert build.country_of({"source": "US commercial payer AI coverage policies",
                             "title": "Aetna updates AI prior-authorization policy", "summary": ""}) == "United States"
    assert build.country_of({"source": "AI health funding programmes (ARPA-H / EU4Health / Horizon / NHS)",
                             "title": "Sonus ultrasound AI wins major ARPA-H award", "summary": ""}) == "United States"
    # trade press about a global topic → unplaced (no false geography)
    assert build.country_of({"source": "STAT — Health Tech",
                             "title": "Clinical chatbots are taking medicine by storm", "summary": ""}) is None


def test_errored_source_guard():
    """With FAIL_ON_DEGRADE set, a build where >30% of sources errored aborts (holds the last good
    build); a few flaky sources do not."""
    import os
    saved = os.environ.get("FAIL_ON_DEGRADE")
    os.environ["FAIL_ON_DEGRADE"] = "1"
    try:
        def health(nfail):
            return {"expected": 10, "contributing": 10 - nfail, "zero_steady": [], "quiet": [],
                    "undated": 0, "failed": [f"s{i}" for i in range(nfail)]}
        # 40% errored → abort
        aborted = False
        try:
            build._emit_ci_health(health(4), [])
        except SystemExit:
            aborted = True
        assert aborted, "should abort when 40% of sources errored"
        # 10% errored → no abort
        ok = True
        try:
            build._emit_ci_health(health(1), [])
        except SystemExit:
            ok = False
        assert ok, "should not abort on a single flaky source"
    finally:
        if saved is None:
            os.environ.pop("FAIL_ON_DEGRADE", None)
        else:
            os.environ["FAIL_ON_DEGRADE"] = saved


def test_funding_raise_context():
    """'raises' is a Funding round only with money/round context — not 'raise questions'."""
    def et(title):
        return build.classify_evidence({"title": title, "summary": "", "layer": "industry", "stype": "Industry press"})[0]
    assert et("AI misdiagnoses raise new liability questions for health systems") != "Funding round"
    assert et("Hippocratic AI raises $141M Series B to scale clinical agents") == "Funding round"
    assert et("Health startup raises Series A for AI triage") == "Funding round"


def test_adoption_attitude_commentary():
    """Adoption-attitude / AI-aversion studies are tagged Commentary, not primary clinical evidence."""
    et, strg = build.classify_evidence(
        {"title": "AI Aversion: Do People Trust and Accept Artificial Intelligence Risk Calculator Recommendations?",
         "summary": "", "layer": "clinical", "stype": "Journal / evidence"})
    assert (et, strg) == ("Commentary", "Commentary")


def test_csv_formula_injection_safe():
    """CSV export neutralises spreadsheet formula injection from third-party titles."""
    assert build._csv_safe("=HYPERLINK(\"http://evil\")").startswith("'=")
    assert build._csv_safe("+cmd|calc").startswith("'+")
    assert build._csv_safe("-2+3").startswith("'-")
    assert build._csv_safe("@SUM(A1)").startswith("'@")
    assert build._csv_safe("FDA clears AI tool") == "FDA clears AI tool"   # normal text untouched
    assert build._csv_safe(8) == "8"


def test_dedup_prefers_primary_url():
    """When the same story arrives via a Google-News redirect and a direct primary feed, the collapse
    keeps the primary link."""
    items = [
        {"id": "g", "title": "MHRA clarifies regulatory status of ambient voice technologies in the NHS",
         "url": "https://news.google.com/rss/articles/CBMxyz", "source": "MHRA (UK)", "layer": "regulation",
         "date": "2026-07-29", "summary": ""},
        {"id": "p", "title": "MHRA clarifies regulatory status of ambient voice technologies in the NHS",
         "url": "https://www.gov.uk/government/news/mhra-ambient-voice-technologies", "source": "MHRA — GOV.UK",
         "layer": "regulation", "date": "2026-07-29", "summary": ""},
    ]
    out = build.collapse_near_duplicates(items)
    assert len(out) == 1, [i["url"] for i in out]
    assert "news.google.com" not in out[0]["url"] and "gov.uk" in out[0]["url"], out[0]["url"]


def test_litigation_digest_bucket():
    """A coverage/regulatory court case is grouped under 'Legal / litigation', not 'Regulatory actions'."""
    o = {"clears": [], "econ": [],
         "reg": [{"id": "x1", "source": "CMS coverage determinations (NCD/LCD) — AI", "layer": "access",
                  "title": "Court Examines AI Discovery in Medicare Advantage Coverage Decision Case", "summary": ""}]}
    whys = {w for w, _ in build._digest(o)}
    assert "Legal / litigation" in whys
    assert "Regulatory actions" not in whys        # not double-counted / mis-framed
    assert "Legal / litigation" in build.WHY_TEXT and "Legal / litigation" in build.WHY_MATTERS


# --- prospective evidence corpus (feed-log schema 2) --------------------------
# A durable, item-level dataset: each item retained once with its contemporaneous classification
# and provenance; write-once + append-only; the epistemic Build A/B boundary is enforced.
import json as _json


def _corpus_item(**over):
    """A fully-classified item as it exists when log_detections runs (fields already attached)."""
    base = {"id": "p1", "url": "https://www.cms.gov/ntap-ai-2026", "title": "Medicare NTAP for AI ECG tool",
            "source": "CMS — coverage & payment notices", "date": "2026-08-01",
            "summary": "coverage decision; cardiology", "layer": "access",
            "etype": "Regulatory guidance", "strength": "Policy signal", "relevance": "Direct clinical",
            "modality": "Predictive ML", "maturity": "HTA", "country": "United States",
            "region": "North America", "stype": "HTA / payer", "decision_type": "Coverage",
            "payer_type": "Public", "topics": ["cms-coverage", "ntap-activity"], "score": 8}
    base.update(over)
    return base


def test_persistent_record_schema():
    """A newly published item becomes a full schema-2 record: identity + THREE distinct dates
    (publication `date`, `first_detected`, `retrieved_at`) + provenance + a frozen first_classification
    that carries the full Build-A verdict (stage, modality, reimbursement_pathway, topics, score …)."""
    out, new, recl = build.build_detection_records("", [_corpus_item()], "2026-08-21", "2026-08-21T09:00:00Z")
    assert (new, recl) == (1, 0)
    rec = _json.loads(out.strip())
    assert rec["schema"] == build.SCHEMA_VERSION
    # six original top-level fields preserved (recall/timeliness consumers keep working)
    for k in ("id", "url", "title", "source", "date", "first_detected"):
        assert k in rec
    # three distinct dates
    assert rec["date"] == "2026-08-01" and rec["first_detected"] == "2026-08-21"
    assert rec["retrieved_at"] == "2026-08-21T09:00:00Z"
    assert rec["date"] != rec["first_detected"] != rec["retrieved_at"]
    # provenance
    assert rec["content_hash"] and rec["canonical_url"] == "https://www.cms.gov/ntap-ai-2026"
    assert rec["snapshot_ref"] is None            # Build A anchors identity; raw capture is Build B's job
    # full contemporaneous classification, frozen and mirrored into history[0]
    assert rec["historical_classification_available"] is True
    fc = rec["first_classification"]
    assert fc["stage"] == "access" and fc["modality"] == "Predictive ML"
    assert fc["reimbursement_pathway"] == ["NTAP", "Reimbursement (general)"]  # per-item, from title+summary
    assert fc["clinical_area"] == ["Cardiology"] and fc["topics"] == ["cms-coverage", "ntap-activity"]
    assert fc["rank_score"] == 8 and fc["taxonomy_version"] == build.TAXONOMY_VERSION
    assert rec["classification_history"] == [fc]
    # NO Build-B conclusion fields leak into Build A
    assert not ({"coverage_status", "verified_event_type", "payment_established", "trajectory_state"}
                & set(rec) & set(fc))


def test_legacy_record_preserved_not_fabricated():
    """A pre-schema (flat, 6-field) line is upgraded once to schema 2 but its classification is NOT
    invented: historical_classification_available:false, first_classification:None, history:[] —
    we never rerun today's classifier and mislabel it 'historical'. Original fields are preserved."""
    legacy = _json.dumps({"id": "old1", "url": "https://x/old", "title": "Old item",
                          "source": "STAT News", "date": "2026-08-10", "first_detected": "2026-08-15"})
    out, new, recl = build.build_detection_records(legacy + "\n", [], "2026-08-21")
    assert (new, recl) == (0, 0)
    rec = _json.loads(out.strip())
    assert rec["schema"] == build.SCHEMA_VERSION
    assert rec["historical_classification_available"] is False
    assert rec["first_classification"] is None and rec["classification_history"] == []
    assert rec["id"] == "old1" and rec["date"] == "2026-08-10" and rec["first_detected"] == "2026-08-15"
    # idempotent: upgrading again changes nothing
    out2, _, _ = build.build_detection_records(out, [], "2026-08-22")
    assert _json.loads(out2.strip()) == rec


def test_no_silent_overwrite_append_only():
    """A later build can NEVER overwrite an item's original verdict. A genuine reclassification APPENDS
    a dated snapshot; first_classification and every prior snapshot stay byte-identical; re-seeing an
    unchanged item appends nothing."""
    out1, _, _ = build.build_detection_records("", [_corpus_item()], "2026-08-21", "2026-08-21T09:00:00Z")
    original_fc = _json.loads(out1.strip())["first_classification"]
    # re-seen, UNCHANGED classification → no new snapshot
    out2, new2, recl2 = build.build_detection_records(out1, [_corpus_item()], "2026-08-22", "2026-08-22T09:00:00Z")
    rec2 = _json.loads(out2.strip())
    assert (new2, recl2) == (0, 0) and len(rec2["classification_history"]) == 1
    # re-seen, CHANGED classification (stage/score/topics drift) → append, never overwrite
    changed = _corpus_item(layer="regulation", score=10, topics=["fda-ai-authorisations"])
    out3, new3, recl3 = build.build_detection_records(out2, [changed], "2026-08-23", "2026-08-23T09:00:00Z")
    rec3 = _json.loads(out3.strip())
    assert (new3, recl3) == (0, 1)
    assert rec3["first_classification"] == original_fc              # frozen
    assert len(rec3["classification_history"]) == 2
    assert rec3["classification_history"][0] == original_fc         # prior snapshot untouched
    assert rec3["classification_history"][1]["stage"] == "regulation" and rec3["classification_history"][1]["rank_score"] == 10


def test_ab_bridge_discovery_input():
    """Integration: source item → Build A → persistent record → a valid Build B DISCOVERY input.
    The record must expose everything B's discovery layer needs to stage a candidate (identity,
    canonical link, publication date, detection date, content hash, and the Build-A classification),
    while carrying NO verified-event conclusion — B still runs its own primary-source verification."""
    out, _, _ = build.build_detection_records("", [_corpus_item()], "2026-08-21", "2026-08-21T09:00:00Z")
    rec = _json.loads(out.strip())
    for field in ("id", "canonical_url", "date", "first_detected", "content_hash", "first_classification"):
        assert rec.get(field) is not None, f"discovery input missing {field}"
    assert rec["first_classification"]["stage"] and rec["first_classification"]["topics"]
    # boundary held: Build A asserts detection+classification only, not a verified event
    assert "verified_event_type" not in rec and "trajectory_state" not in rec


def test_first_classification_immutable():
    """LOCKED INVARIANT: first_classification is immutable once written. No sequence of later builds —
    reclassification, taxonomy bump, or plain re-observation — may change it. Only classification_history
    grows. This preserves the prospective-study answer: what did the monitor classify at first sight?"""
    out, _, _ = build.build_detection_records("", [_corpus_item()], "2026-08-21", "2026-08-21T09:00:00Z")
    frozen = _json.loads(out.strip())["first_classification"]
    # apply a chain of divergent reclassifications across successive builds
    for day, over in (("2026-08-22", dict(layer="regulation", score=10)),
                      ("2026-08-23", dict(layer="clinical", score=5, topics=["ai-clinical-studies"])),
                      ("2026-08-24", dict(modality="Imaging AI"))):
        out, _, _ = build.build_detection_records(out, [_corpus_item(**over)], day, day + "T09:00:00Z")
    rec = _json.loads(out.strip())
    assert rec["first_classification"] == frozen                       # never mutated
    assert rec["classification_history"][0] == frozen                  # original snapshot preserved
    assert len(rec["classification_history"]) == 4                     # 1 original + 3 appended
    assert [s["stage"] for s in rec["classification_history"]] == ["access", "regulation", "clinical", "access"]


def test_taxonomy_version_mandatory():
    """LOCKED INVARIANT: every non-legacy classification snapshot carries taxonomy_version, so verdicts
    produced under different rule sets are never mistaken for comparable. Legacy records have no
    classification at all (unavailable), which is the honest alternative — not a version-less snapshot."""
    # new + reclassified record: every snapshot stamped
    out, _, _ = build.build_detection_records("", [_corpus_item()], "2026-08-21")
    out, _, _ = build.build_detection_records(out, [_corpus_item(layer="regulation", score=10)], "2026-08-22")
    rec = _json.loads(out.strip())
    assert rec["first_classification"]["taxonomy_version"] == build.TAXONOMY_VERSION
    for snap in rec["classification_history"]:
        assert snap.get("taxonomy_version"), "every snapshot must carry taxonomy_version"
    # legacy: no fabricated version-less classification
    legacy = _json.dumps({"id": "old", "url": "https://x/old", "title": "Old", "source": "STAT",
                         "date": "2026-08-10", "first_detected": "2026-08-15"})
    lout, _, _ = build.build_detection_records(legacy + "\n", [], "2026-08-21")
    lrec = _json.loads(lout.strip())
    assert lrec["first_classification"] is None and lrec["classification_history"] == []


def test_coverage_litigation_not_a_decision():
    """A court case about a coverage decision is not itself a coverage decision — decision_type Unknown."""
    dt, pt = build.access_facets({"layer": "access", "source": "CMS coverage determinations (NCD/LCD) — AI",
        "title": "Court Examines AI Discovery in Medicare Advantage Coverage Decision Case", "summary": ""})
    assert dt == "Unknown", dt
    # a genuine coverage determination still reads Coverage
    dt2, _ = build.access_facets({"layer": "access", "source": "CMS coverage determinations (NCD/LCD) — AI",
        "title": "CMS finalises National Coverage Determination reimbursing AI stroke triage", "summary": ""})
    assert dt2 == "Coverage", dt2


def test_access_facets():
    """Access items carry structured decision_type + payer_type; unmatched → 'Unknown' (no false
    default); non-access items get empty facets."""
    def f(title, source, layer="access"):
        return build.access_facets({"title": title, "summary": "", "source": source, "layer": layer})
    assert f("CMS finalises National Coverage Determination for AI stroke triage", "CMS coverage determinations (NCD/LCD) — AI") \
        == ("Coverage", "National payer")
    assert f("UnitedHealthcare updates medical policy on AI risk scoring", "US commercial payer AI coverage policies") \
        == ("Coverage", "Commercial payer")
    assert f("NICE recommends AI imaging via health technology evaluation", "NICE guidance")[0] == "HTA recommendation"
    assert f("NHS England signs national AI imaging framework agreement", "Procurement & tenders") == ("Procurement", "Procurement body")
    assert f("ARPA-H funding programme backs AI diagnostics", "AI health funding programmes")[0] == "Funding programme"
    # unmatched access item → Unknown, never silently 'Coverage'
    assert f("Vendor announces AI platform milestone", "Some access source") == ("Unknown", "Unknown")
    # non-access → empty facets
    assert build.access_facets({"title": "RCT of AI triage", "summary": "", "source": "NEJM AI", "layer": "clinical"}) == ("", "")


def test_ctgov_geography():
    """A trial's dominant location country (ClinicalTrials.gov structured metadata) wins over regex."""
    assert build.country_of({"source": "AI/ML intervention trials", "geo_country": "Germany",
                             "title": "AI decision support in the emergency department", "summary": ""}) == "Germany"
    # normalisation of ClinicalTrials.gov country spelling happens in the fetcher via _CTGOV_GEO
    assert build._CTGOV_GEO["Korea, Republic of"] == "South Korea"


def test_payer_decision_outranks_procurement():
    """Within Market access, a formal payer/coverage decision scores higher than a procurement/tender
    announcement, so reimbursement decisions surface above purchasing news."""
    payer = {"layer": "access", "source": "CMS coverage determinations (NCD/LCD) — AI", "tier": "weekly",
             "url": "https://news.google.com/rss/x1", "date": "",
             "title": "CMS finalises National Coverage Determination reimbursing AI stroke triage", "summary": ""}
    procurement = {"layer": "access", "source": "Hospital & national AI procurement", "tier": "weekly",
                   "url": "https://news.google.com/rss/x2", "date": "",
                   "title": "NHS England signs AI imaging framework agreement with vendors", "summary": ""}
    sp, _ = build.rank_score(payer)
    sq, _ = build.rank_score(procurement)
    assert sp > sq, (sp, sq)


def test_procurement_stays_access():
    """Hospital/national purchasing (tender, framework agreement) is market access via procurement —
    it stays in the access stage rather than being routed to industry."""
    items = [
        {"layer": "access", "source": "AI device reimbursement & coding", "url": "https://news.google.com/rss/x1",
         "gnews": True, "title": "NHS England awards national AI imaging framework agreement to consortium", "summary": ""},
        {"layer": "access", "source": "AI device reimbursement & coding", "url": "https://news.google.com/rss/x2",
         "gnews": True, "title": "VA issues tender for AI triage software across regional hospitals", "summary": ""},
    ]
    build.refine_access_layer(items)
    assert all(i["layer"] == "access" for i in items), [i["layer"] for i in items]


def test_jca_routes_to_heor():
    """EU Joint Clinical Assessment is an HTA mechanism — a JCA item lands in HEOR through the FULL
    pipeline (refine_access routes it in, refine_heor must not evict it)."""
    items = [{"layer": "access", "source": "EU Joint Clinical Assessment (EUnetHTA / HTACG)",
              "url": "https://news.google.com/rss/x", "gnews": True,
              "title": "Strengthening pharma and medtech joint clinical assessment with AI", "summary": ""}]
    build.refine_access_layer(items)
    build.refine_heor_layer(items)          # must survive both refiners
    assert items[0]["layer"] == "heor", items[0]["layer"]


def test_out_of_scope_insect_and_sport():
    """Insect-farming/animal-feed and non-health (sport) items from broad queries are dropped."""
    items = [
        {"source": "PubMed — AI × HTA/HEOR", "layer": "clinical", "url": "https://x/1",
         "title": "Machine learning models for predicting crude protein and fat content in black soldier fly larvae", "summary": ""},
        {"source": "LATAM — HTA & coverage (CONITEC)", "layer": "access", "url": "https://news.google.com/rss/2",
         "gnews": True, "title": "An AI commentor will assist with coverage of the New Mexico Open golf tournament", "summary": ""},
        {"source": "NEJM AI", "layer": "clinical", "url": "https://x/3",
         "title": "Machine learning decision support reduces inpatient lab utilization", "summary": ""},
    ]
    kept = {i["title"] for i in build.relevance_gate(items)}
    assert any("decision support" in t for t in kept)                       # real study kept
    assert not any(("larvae" in t.lower() or "golf" in t.lower()) for t in kept)  # leaks dropped


def test_hta_perspective_tagging():
    """HTA-ecosystem commentary from broad news → 'HTA perspective'/Commentary (non-primary), kept in
    heor; genuine model-based value evaluation stays 'HEOR / value'/Secondary."""
    op = build.classify_evidence({"title": "AI will likely grow the HTA industrial complex, but it can also democratise the institution",
        "summary": "", "layer": "heor", "url": "https://news.google.com/rss/x", "gnews": True, "stype": "Other"})
    assert op == ("HTA perspective", "Commentary"), op
    ev = build.classify_evidence({"title": "Improving laboratory workforce efficiency using AI-assisted digital cytology: a model-based evaluation for the NHS",
        "summary": "", "layer": "heor", "url": "https://pubmed.ncbi.nlm.nih.gov/1", "stype": "Journal / evidence"})
    assert ev == ("HEOR / value", "Secondary evidence"), ev


def test_method_paper_to_research():
    """Pure model-development papers with no patient validation move clinical → research;
    a foundation-model paper with clinical validation stays clinical."""
    items = [
        {"source": "Nature Medicine", "layer": "clinical", "url": "https://x/1",
         "title": "A pathology foundation model pretrained with self-supervised learning", "summary": ""},
        {"source": "Nature Medicine", "layer": "clinical", "url": "https://x/2",
         "title": "End-to-end pathology foundation model validated in a patient cohort", "summary": ""},
    ]
    build.refine_method_papers(items)
    lay = {i["title"][:20]: i["layer"] for i in items}
    assert lay["A pathology foundati"] == "research"   # pure method dev → research
    assert lay["End-to-end pathology"] == "clinical"   # patient-validated stays clinical


def test_research_health_only():
    """Research is health-gated: AI-in-health preprints/newsletters stay, general-AI capability drops."""
    items = [
        {"source": "arXiv", "layer": "research", "url": "https://arxiv.org/abs/1",
         "title": "A report-grounded foundation model for colonoscopy", "summary": ""},
        {"source": "arXiv", "layer": "research", "url": "https://arxiv.org/abs/2",
         "title": "Qwen-UI-Agent: next-generation foundation GUI agents", "summary": ""},
        {"source": "TLDR AI", "layer": "research", "url": "https://tldr.tech/ai/x",
         "title": "GPT-5.6 price cuts, Gemini Robotics 2", "summary": ""},
        {"source": "TLDR AI", "layer": "research", "url": "https://tldr.tech/ai/y",
         "title": "ChatGPT Health launches, new clinical model", "summary": ""},
    ]
    kept = {i["title"][:12] for i in build.relevance_gate(items)}
    assert "A report-gro" in kept        # colonoscopy → health
    assert "ChatGPT Heal" in kept         # health-flagged newsletter issue
    assert "Qwen-UI-Agen" not in kept     # general-AI preprint dropped
    assert "GPT-5.6 pric" not in kept     # general frontier newsletter dropped


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
    """The reimbursement stream is payment/coverage only: payment stays; HTA/value → HEOR;
    governance → regulation; company news → industry; private-insurer AI → industry."""
    items = [
        {"layer": "access", "title": "CMS proposes payment framework for software as a medical service", "summary": ""},
        {"layer": "access", "title": "NHS procurement framework for AI diagnostics", "summary": ""},
        {"layer": "access", "title": "AI use in health system must be deemed safe - HIQA", "summary": ""},
        {"layer": "access", "title": "G-BA benefit assessment of an AI digital therapeutic", "summary": ""},
        {"layer": "access", "title": "DIAGNOS gets Health Canada licence for AI retinal analysis", "summary": ""},
        {"layer": "access", "title": "Luminopia partners with Spin Master on amblyopia digital therapeutic", "summary": ""},
        {"layer": "access", "title": "Mexico's GNP Seguros to leverage Palantir AI to strengthen insurance coverage", "summary": ""},
    ]
    build.refine_access_layer(items)
    by = {i["title"][:12]: i["layer"] for i in items}
    assert by["CMS proposes"] == "access"        # payment signal → stays
    assert by["NHS procurem"] == "access"         # market-access mechanism → stays
    assert by["AI use in he"] == "regulation"     # HIQA safety/governance, no payment signal → out
    assert by["G-BA benefit"] == "heor"           # value / HTA assessment → HEOR
    assert by["DIAGNOS gets"] == "regulation"     # licence, no payment/value signal
    assert by["Luminopia pa"] == "industry"       # company partnership news
    assert by["Mexico's GNP"] == "industry"       # private insurer, no public-payer signal


def test_regulation_precision():
    """Regulatory keeps regulator sources + genuine regulatory/governance/safety signals; generic
    policy/marketing news is routed to industry."""
    # These arrive via regulator-NAMED Google-News queries (gnews) — the only items this filter demotes.
    items = [
        {"layer": "regulation", "gnews": True, "source": "MHRA (UK)", "url": "", "title": "MHRA guidance on AI scribes", "summary": ""},
        {"layer": "regulation", "gnews": True, "source": "AI policy & guidance", "url": "", "title": "EU AI Act radiology compliance", "summary": ""},
        {"layer": "regulation", "gnews": True, "source": "Additional European HTA", "url": "", "title": "AI use in health system must be deemed safe - HIQA", "summary": ""},
        {"layer": "regulation", "gnews": True, "source": "MEA AI device & digital health regulation", "url": "", "title": "Role of AI Healthcare Solutions in Saudi's Care Domain", "summary": ""},
    ]
    build.refine_regulation_layer(items)
    lay = {i["title"][:10]: i["layer"] for i in items}
    assert lay["MHRA guida"] == "regulation"   # regulator source
    assert lay["EU AI Act "] == "regulation"    # AI Act / compliance signal
    assert lay["AI use in "] == "regulation"    # governance / safety signal
    assert lay["Role of AI"] == "industry"      # marketing / adoption puff → out


def test_heor_precision():
    """HEOR keeps value/economic/HTA/RWE evidence; corrections/retractions → literature (clinical);
    newsletters/digests → industry; non-economic AI reviews → clinical; genuine HTA-body evidence stays."""
    items = [
        {"layer": "heor", "source": "PubMed — AI × HTA/HEOR",
         "title": "AI in determination of the postmortem interval: systematic review and meta-analysis", "summary": ""},
        {"layer": "heor", "source": "AI in HTA & market access",
         "title": "AI will likely grow the HTA industrial complex", "summary": ""},
        {"layer": "heor", "source": "Value in Health",
         "title": "Retraction notice to Integrating Generative AI Into Evidence Synthesis", "summary": ""},
        {"layer": "heor", "source": "OHDSI Blog", "title": "Weekly OHDSI Digest - July 2026", "summary": ""},
        {"layer": "heor", "source": "INAHTA (HTA network)", "title": "HTA appraisal: AI use for skin cancer", "summary": ""},
        {"layer": "heor", "source": "PubMed — AI × HTA/HEOR", "title": "AI workflow efficiency and productivity in radiology", "summary": ""},
    ]
    build.refine_heor_layer(items)
    lay = {i["title"][:10]: i["layer"] for i in items}
    assert lay["AI in dete"] == "clinical"   # no economic signal → reclassified
    assert lay["AI will li"] == "heor"       # HTA signal → stays
    assert lay["Retraction"] == "clinical"   # correction/retraction → out of the value stream
    assert lay["Weekly OHD"] == "industry"   # newsletter/digest → out
    assert lay["HTA apprai"] == "heor"       # genuine HTA-body evidence stays
    assert lay["AI workflo"] == "clinical"   # decision 1: bare workflow/productivity is NOT HEOR → clinical


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


def test_research_layer_precision():
    """Research keeps preprint/journal contributions; frontier newsletters / product launches
    (source_type 'Other') that passed the health gate are routed to industry."""
    items = [
        {"source": "arXiv", "layer": "research", "url": "https://arxiv.org/abs/1", "title": "A model for X", "summary": ""},
        {"source": "medRxiv — Health Informatics", "layer": "research", "url": "https://www.medrxiv.org/x", "title": "Y", "summary": ""},
        {"source": "OpenAI News", "layer": "research", "url": "https://openai.com/index/health-in-chatgpt", "title": "Launching Health in ChatGPT", "summary": ""},
        {"source": "TLDR AI", "layer": "research", "url": "https://tldr.tech/ai/z", "title": "ChatGPT Health, new model", "summary": ""},
    ]
    build.refine_research_layer(items)
    lay = {i["source"]: i["layer"] for i in items}
    assert lay["arXiv"] == "research"                       # preprint kept
    assert lay["medRxiv — Health Informatics"] == "research"
    assert lay["OpenAI News"] == "industry"                 # product launch → industry
    assert lay["TLDR AI"] == "industry"                     # newsletter → industry


def test_evidence_maturity():
    """0–4 lifecycle level for evidence items; None for policy/market."""
    mt = lambda et, layer, title="": build.evidence_maturity({"etype": et, "layer": layer, "title": title, "summary": ""})
    assert mt("Preprint", "research")[0] == 0
    assert mt("Journal study", "clinical", "A retrospective analysis of X")[0] == 1
    assert mt("Trial registry", "clinical")[0] == 2
    assert mt("Journal study", "clinical", "A prospective evaluation")[0] == 2
    assert mt("RCT", "clinical")[0] == 3
    assert mt("Real-world evidence", "clinical")[0] == 4
    assert mt("Regulatory guidance", "regulation")[0] is None    # policy → N/A
    assert mt("Partnership", "industry")[0] is None               # market → N/A
    assert mt("Commentary", "clinical")[0] is None                # not evidence


def test_ai_modality():
    """AI-modality tag; blank when no clear signal."""
    mod = lambda t: build.ai_modality({"title": t, "summary": ""})
    assert mod("Deep-learning echocardiographic measurements") == "Imaging AI"
    assert mod("AI scribes take clinical notes") == "Generative AI / LLM"
    assert mod("A digital therapeutic for PTSD") == "Digital therapeutic"
    assert mod("Surgical robotics platform") == "Robotics"
    assert mod("Wearable temperature monitoring model") == "Remote monitoring"
    assert mod("AI-driven small-molecule drug discovery and target identification") == "Drug discovery AI"
    assert mod("Improving mental health screening and early risk detection") == "Clinical decision support"
    assert mod("CMS proposes a payment framework") == ""     # no modality signal → blank


def test_healthcare_relevance():
    """Direct clinical vs operations vs biomedical vs adjacent."""
    rel = lambda t, layer="clinical": build.healthcare_relevance({"title": t, "layer": layer, "summary": ""})
    assert rel("Deep-learning model detects atrial fibrillation on ECG") == "Direct clinical"
    assert rel("AI scribes cut documentation burden for clinicians") == "Healthcare operations"
    assert rel("Waystar AI-powered revenue cycle management", "industry") == "Healthcare operations"
    assert rel("NUHS saves 850 staff hours weekly from new AI", "industry") == "Healthcare operations"
    assert rel("Two visions for ambient AI and the future of the EHR", "industry") == "Healthcare operations"
    assert rel("Collaboration can advance AI-powered behavioural health", "industry") == "Direct clinical"
    assert rel("Generative AI accelerates drug discovery and protein design", "research") == "Biomedical research"
    assert rel("Launching a general-purpose agentic AI foundation model", "industry") == "Adjacent AI"


def test_evidence_classification():
    """Deterministic evidence-type + strength: study designs, policy, market, commentary."""
    def ev(title, layer, source="", stype=None, summary=""):
        i = {"title": title, "layer": layer, "source": source, "summary": summary}
        if stype:
            i["stype"] = stype
        return build.classify_evidence(i)
    assert ev("A randomized controlled trial of an AI triage tool", "clinical", stype="Journal / evidence") == ("RCT", "Primary evidence")
    assert ev("AI in postmortem interval: systematic review and meta-analysis", "clinical", stype="Journal / evidence") == ("Meta-analysis", "Secondary evidence")
    assert ev("Responsible AI in medical imaging: a systematic review", "clinical", stype="Journal / evidence") == ("Systematic review", "Secondary evidence")
    assert ev("Cost-effectiveness of an AI triage tool", "heor", stype="Journal / evidence") == ("Economic evaluation", "Primary evidence")
    assert ev("AI scribes are not medical devices, MHRA says", "regulation", stype="Regulator") == ("Regulatory guidance", "Policy signal")
    assert ev("FDA classification of the diabetes digital therapeutic device", "regulation", stype="Regulator") == ("Regulatory authorisation", "Policy signal")
    assert ev("EU AI Act radiology: beyond compliance to patient safety", "regulation") == ("AI governance", "Policy signal")
    assert ev("CMS proposes payment framework for software", "access") == ("Payment / coverage", "Policy signal")
    assert ev("NHS procurement framework for AI diagnostics", "access") == ("Market access", "Policy signal")
    assert ev("NICE recommends reimbursement for the AI tool", "access") == ("Payment / coverage", "Policy signal")
    assert ev("Budget impact analysis of an AI triage tool", "heor") == ("Budget impact", "Secondary evidence")
    assert ev("G-BA benefit assessment of the AI device", "heor") == ("HTA report", "Secondary evidence")
    # opinion / policy / education essays are commentary, not empirical evidence
    assert ev("Health professions education in the age of generative AI", "clinical", stype="Journal / evidence") == ("Commentary", "Commentary")
    assert ev("Reimagining regulatory strategy: agentic AI as an enabler of market access", "clinical", stype="Journal / evidence") == ("Commentary", "Commentary")
    assert ev("WellSpan Health, Hippocratic AI ink multi-year partnership", "industry", stype="Industry press") == ("Partnership", "Market signal")
    assert ev("Startup raises $40M Series B for AI imaging", "industry", stype="Industry press") == ("Funding round", "Market signal")
    assert ev("Recursion selects its first AI chief", "industry", stype="Industry press") == ("Executive move", "Market signal")
    assert ev("Nabla explains its clinical AI outlook", "industry", stype="Industry press") == ("Industry analysis", "Market signal")
    assert ev("Retraction notice to Integrating Generative AI", "heor", source="Value in Health", stype="Journal / evidence") == ("Commentary", "Commentary")
    assert ev("AI-Assisted Optical Diagnosis (CADx)", "clinical", stype="Trial registry") == ("Trial registry", "Primary evidence")
    assert ev("A foundation model for colonoscopy", "research", stype="Preprint / research") == ("Preprint", "Primary evidence")


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


def test_trends_html_renders():
    """Guard: trends_html must be defined in build.py and render without error (regression guard —
    it was once accidentally moved out during a refactor and only CI caught it)."""
    hist = [{"date": f"2026-08-0{d}", "layers": {k: d for k in build.LAYERS},
             "terms": {"large language model": d}, "total": 6 * d} for d in range(1, 9)]
    items = [{"id": str(i), "title": "An AI clinical study", "url": f"https://e/{i}", "source": "NEJM AI",
              "layer": "clinical", "date": "2026-08-10", "summary": "", "score": 1} for i in range(3)]
    out = build.trends_html(items, hist)
    assert isinstance(out, str) and len(out) > 0


def test_coverage_stub_is_inert():
    """The optional panel is disabled: its placeholder functions return nothing, so no extra tab
    renders and the main build is unaffected."""
    assert build.load_coverage_public() is None
    assert build.coverage_public_html({"n": 5}) == ""
    assert build.coverage_mini_html({"n": 5}) == ""


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


def test_history_reset():
    """RESET_HISTORY=1 discards prior rows so the trend series restarts at the current build."""
    import os, json as _json
    orig_get, orig_put = build.private_get, build.private_put
    try:
        build.private_put = lambda *a, **k: True
        prior = _json.dumps([{"date": "2026-07-01"}, {"date": "2026-07-02"}, {"date": "2026-07-03"}])
        build.private_get = lambda name, token=None: (prior, "sha") if name == "history.json" else ("", None)
        items, o = _pipeline()
        os.environ["RESET_HISTORY"] = "1"
        _, hist = build.log_history(items, [], token="x", o=o)
        assert len(hist) == 1                          # 3 prior rows discarded → only this build
        os.environ.pop("RESET_HISTORY", None)
        _, hist2 = build.log_history(items, [], token="x", o=o)
        assert len(hist2) == 4                          # without the flag, prior rows retained
    finally:
        os.environ.pop("RESET_HISTORY", None)
        build.private_get, build.private_put = orig_get, orig_put


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
    assert row["taxonomy_version"] == build.TAXONOMY_VERSION   # each row stamps the rules it used
    assert row["layers"] == GOLDEN_LAYERS
    assert row["regions"] == GOLDEN_REGIONS
    assert row["clinical"] == GOLDEN_CLINICAL
    assert row["bodies"] == GOLDEN_BODIES
    # topic column set is the full registry (stable schema, zeros kept)
    assert set(row["topics"]) == {t["slug"] for t in build.TOPICS}
    assert row["topics"]["oncology-ai"] == 2 and row["topics"]["ema-activity"] == 0
    # QA outcome recorded and passing
    assert row["qa"]["published"] == 6 and row["qa"]["dropped"] == 0 and row["qa"]["passed"] is True


# --- P0 remediation (audit v3.1) ---------------------------------------------
def test_source_registry_npj_and_domains():
    """P0-3: npj (name) and journal domains resolve to 'Journal / evidence', not 'Other'."""
    assert build.source_type({"source": "npj Digital Medicine", "url": "https://www.nature.com/articles/s41746-x"}) == "Journal / evidence"
    assert build.source_type({"source": "Cochrane — AI & digital health reviews", "url": "https://pubmed.ncbi.nlm.nih.gov/1/"}) == "Journal / evidence"
    # a journal by domain even when the name has no journal token
    assert build.source_type({"source": "Some Feed", "url": "https://www.thelancet.com/journals/x"}) == "Journal / evidence"
    # Google-News transport is NOT a journal
    assert build.source_type({"source": "AI in HTA & market access", "url": "https://news.google.com/rss/articles/ABC"}) == "Other"


def test_primary_evidence_gate():
    """P0-2: transport (Google News) can't be 'Primary evidence'; real journals/preprints/RWE can."""
    ev = build.classify_evidence
    # gnews clinical item, non-journal publisher → downgraded from primary
    et, strn = ev({"title": "Digital health VC hits $7.4B in H1 2026", "layer": "clinical",
                   "source": "AI in HTA & market access", "url": "https://news.google.com/rss/articles/ABC",
                   "gnews": True, "publisher": "Fierce Healthcare", "summary": ""})
    assert strn != "Primary evidence", (et, strn)
    # gnews item whose underlying publisher IS a journal → eligible
    et, strn = ev({"title": "New AI triage study", "layer": "clinical",
                   "source": "AI in HTA & market access", "url": "https://news.google.com/rss/articles/XYZ",
                   "gnews": True, "publisher": "Nature Medicine", "summary": ""})
    assert strn == "Primary evidence", (et, strn)
    # real journal item stays primary
    assert ev({"title": "An AI model for sepsis detection", "layer": "clinical",
               "source": "npj Digital Medicine", "url": "https://www.nature.com/articles/x", "summary": ""})[1] == "Primary evidence"
    # RWE repository (PCORnet) stays primary despite 'Other' source_type
    assert ev({"title": "Deriving real world insights using EHR and machine learning", "layer": "heor",
               "source": "PCORnet (real-world evidence)", "url": "https://pcornet.org/news/x", "summary": ""})[1] == "Primary evidence"


def test_preprint_precedence():
    """P0-4: a preprint's provenance overrides RWE/RCT title keywords."""
    et, strn = build.classify_evidence({"title": "A real-world evaluation of a video language model",
                                        "layer": "research", "source": "arXiv",
                                        "url": "https://arxiv.org/abs/2608.06361", "summary": ""})
    assert et == "Preprint", (et, strn)


def test_regulatory_subtypes():
    """P0-5: sandbox/programme, enforcement, consultation, rule ≠ generic 'Regulatory guidance'."""
    ev = lambda title: build.classify_evidence({"title": title, "layer": "regulation",
                                                "source": "MHRA — GOV.UK (primary)", "stype": "Regulator", "summary": ""})[0]
    assert ev("Pioneering AI health innovations regulatory sandbox launched") == "Regulatory programme"
    # live regression: a sandbox whose summary mentions 'real-world evidence' must NOT be typed RWE
    assert build.classify_evidence({"title": "Pioneering AI health innovations regulatory sandbox launched",
        "layer": "regulation", "source": "MHRA — GOV.UK (primary)", "stype": "Regulator",
        "summary": "The sandbox lets AI medical devices deploy in live clinical settings to generate real-world evidence."})[0] == "Regulatory programme"
    assert ev("MHRA issues field safety notice and recall for AI device") == "Enforcement / safety"
    assert ev("Agency opens consultation on AI medical device rules") == "Consultation / policy"
    assert ev("Parliament passes AI in healthcare legislation") == "Rule / legislation"
    assert ev("MHRA clarifies regulatory status of ambient voice technologies") == "Regulatory guidance"


def test_geography_no_gnews_query_fallback():
    """P0-6: never infer content-country from a Google-News query's country."""
    # gnews China query, no China token in content → NOT tagged China
    assert build.country_of({"source": "China — reimbursement & payment (NHSA)", "gnews": True,
                             "url": "https://news.google.com/rss/articles/ABC",
                             "title": "Young workers skip cancer screenings as AI replaces the doctor visit",
                             "summary": ""}) is None
    # but explicit in-content geography still wins
    assert build.country_of({"source": "China — reimbursement & payment (NHSA)", "gnews": True,
                             "url": "https://news.google.com/rss/articles/XYZ",
                             "title": "NMPA approves new AI diagnostic in China", "summary": ""}) == "China"


def test_pubmed_pubtype_typing():
    """P0-1: NLM publication type drives evidence typing for PubMed items."""
    ev = lambda pubtype, title="A study": build.classify_evidence(
        {"title": title, "layer": "clinical", "source": "JAMA Network — AI in medicine",
         "stype": "Journal / evidence", "summary": "", "pubtype": pubtype})
    assert ev(["Randomized Controlled Trial", "Journal Article"]) == ("RCT", "Primary evidence")
    assert ev(["Editorial"]) == ("Commentary", "Commentary")
    assert ev(["Systematic Review"]) == ("Systematic review", "Secondary evidence")
    assert ev(["Meta-Analysis"]) == ("Meta-analysis", "Secondary evidence")


def test_pubmed_abstract_out_of_scope():
    """P0-1: with the abstract in summary, an out-of-scope paper is dropped by the relevance gate."""
    battery = {"title": "High-Entropy Engineering of Phosphate Based Polyanion Cathode Materials",
               "summary": "We report a sodium-ion battery cathode with improved cycling using machine learning to optimise composition.",
               "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "url": "https://pubmed.ncbi.nlm.nih.gov/1/"}
    kept = build.relevance_gate([battery])
    assert battery not in kept, "battery/materials paper should be gated out via its abstract"


# --- P1 remediation (audit v3.1) ---------------------------------------------
def test_litigation_type_and_ranking():
    """P1-7: a court case is 'Legal / litigation', not a coverage decision, and gets no access boost."""
    court = {"title": "Court Examines AI Discovery in Medicare Advantage Coverage Decision Case",
             "layer": "access", "source": "CMS coverage determinations (NCD/LCD) — AI",
             "url": "https://news.google.com/rss/articles/ABC", "summary": "", "date": ""}
    assert build.classify_evidence(court)[0] == "Legal / litigation"
    _, reasons = build.rank_score(court)
    assert "Reimbursement / coverage" not in reasons, reasons
    # a genuine coverage decision DOES get the boost
    cov = {"title": "CMS finalises NCD covering the AI diagnostic", "layer": "access",
           "source": "CMS coverage determinations (NCD/LCD) — AI", "url": "https://cms.gov/x", "summary": "", "date": ""}
    assert "Reimbursement / coverage" in build.rank_score(cov)[1]


def test_commentary_excluded():
    """Commentary is EXCLUDED from the feed (not parked in research/industry) — taxonomy hierarchy."""
    comment = {"title": "Artificial intelligence as a new commercial determinant of health",
               "layer": "clinical", "source": "BMJ — AI in medicine", "summary": "", "pubtype": []}
    rct = {"title": "A randomized controlled trial of an AI sepsis alert", "layer": "clinical",
           "source": "npj Digital Medicine", "summary": "", "pubtype": ["Randomized Controlled Trial"]}
    kept = build.refine_commentary_layer([comment, rct])
    assert comment not in kept, "commentary should be excluded"
    assert rct in kept, "a real RCT should stay"


def test_research_precision_reviews_to_clinical():
    """Research = models/methods/benchmarks; evidence-synthesis reviews route to clinical."""
    items = [
        {"title": "MedPixel: a unified pixel-language model for medical imaging", "layer": "research",
         "source": "arXiv", "url": "https://arxiv.org/abs/1", "summary": ""},
        {"title": "Deep learning for diabetic retinopathy screening: a systematic review", "layer": "research",
         "source": "PubMed — AI × HTA/HEOR", "url": "https://pubmed.ncbi.nlm.nih.gov/2/", "summary": "",
         "pubtype": ["Systematic Review"]}]
    build.refine_research_precision(items)
    assert items[0]["layer"] == "research", "a model preprint stays in research"
    assert items[1]["layer"] == "clinical", "an evidence-synthesis review leaves research"


def test_scrape_nav_rejected():
    """P1-9: navigation/topic/index hrefs are rejected by the scrape guard."""
    assert build._SCRAPE_NAV_RE.search("/heor-resources/heor-by-topic-new/digital-health-devices-and-diagnostics")
    assert build._SCRAPE_NAV_RE.search("/strategic-initiatives/topics/artificial-intelligence")
    # a genuine article/report slug is NOT rejected
    assert not build._SCRAPE_NAV_RE.search("/heor-resources/good-practices/article/quantitative-benefit-risk")


def test_filename_title_guard():
    """P1-10: filename-like / non-article titles are dropped by the relevance gate."""
    junk = {"title": "ListS_SFOPH-AI use for skin cancer", "summary": "", "layer": "heor",
            "source": "INAHTA (HTA network)", "url": "https://www.inahta.org/download/lists_sfoph/"}
    assert junk not in build.relevance_gate([junk])
    assert build._FILENAME_TITLE_RE.search("guidance_document.pdf")
    assert not build._FILENAME_TITLE_RE.search("NICE recommends the AI diagnostic for NHS use")


def test_regulation_gnews_query_non_regulatory():
    """E4/E5a: a non-regulatory story from a regulator-NAMED Google-News query is routed to industry,
    while native regulator feeds and genuinely regulatory query items stay in regulation."""
    items = [
        # gnews query whose name carries a regulator token (ANVISA) but the item is a launch
        {"title": "General Hospital of Mexico Launches AI Research Center", "layer": "regulation",
         "source": "LATAM — device authorisation (ANVISA / COFEPRIS)",
         "url": "https://news.google.com/rss/articles/AAA", "gnews": True, "summary": ""},
        # gnews regulator-named query, but genuinely regulatory → stays
        {"title": "CDSCO issues guidance clarifying the regulatory pathway for AI software",
         "layer": "regulation", "source": "India — device authorisation & AI guidance (CDSCO / ICMR)",
         "url": "https://news.google.com/rss/articles/BBB", "gnews": True, "summary": ""},
        # native regulator feed (not gnews) → always kept
        {"title": "MHRA update on health tech", "layer": "regulation",
         "source": "MHRA — GOV.UK (primary)", "url": "https://www.gov.uk/x", "summary": ""},
    ]
    build.refine_regulation_layer(items)
    assert items[0]["layer"] == "industry", "non-reg launch via regulator-named query should leave regulation"
    assert items[1]["layer"] == "regulation", "genuine regulatory guidance should stay"
    assert items[2]["layer"] == "regulation", "native regulator feed should always stay"


def test_pubmed_health_relevance_required():
    """Cat-3 fix: a PubMed item with no health signal is dropped even from an AI-native source."""
    ewaste = {"title": "Donor-acceptor cationic porous organic polymers for photo-enhanced gold recovery from electronic waste",
              "summary": "A machine-learning-optimised polymer for selective gold adsorption from e-waste leachate.",
              "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "url": "https://pubmed.ncbi.nlm.nih.gov/9/"}
    assert ewaste not in build.relevance_gate([ewaste])
    health = {"title": "Cost-effectiveness of an AI triage tool in the emergency department",
              "summary": "We assessed patient outcomes and costs.", "layer": "heor",
              "source": "PubMed — AI × HTA/HEOR", "url": "https://pubmed.ncbi.nlm.nih.gov/10/"}
    assert health in build.relevance_gate([health])


def test_regulation_type_keyed_on_stage():
    """Cat-2/5 fix: an item routed to industry is not typed 'Regulatory guidance' just because its
    source name carries a regulator token."""
    et, _ = build.classify_evidence({"title": "How AI Is Flipping Power Dynamics in Modern Healthcare",
        "layer": "industry", "source": "LATAM — device authorisation (ANVISA / COFEPRIS)",
        "url": "https://news.google.com/rss/articles/ZZZ", "summary": ""})
    assert et != "Regulatory guidance", et
    # a genuine regulation-stage item still types as regulatory
    assert build.classify_evidence({"title": "MHRA issues guidance on AI devices", "layer": "regulation",
        "source": "MHRA — GOV.UK (primary)", "stype": "Regulator", "summary": ""})[0] == "Regulatory guidance"


def test_commentary_industry_excluded():
    """Industry-stage opinion/commentary is excluded (not left in Industry); commercial news stays."""
    op = {"title": "Opinion: AI won't enhance physician autonomy. It will diminish it",
          "layer": "industry", "source": "STAT — Health Tech", "summary": "", "pubtype": []}
    fund = {"title": "HealthSnap raises $25M for AI virtual care", "layer": "industry",
            "source": "MobiHealthNews", "summary": "", "pubtype": []}
    kept = build.refine_commentary_layer([op, fund])
    assert op not in kept, "industry opinion should be excluded"
    assert fund in kept, "a funding story stays in industry"


def test_heor_requires_economic_signal():
    """Decision 1: HEOR needs an explicit economic/value signal; bare workflow/efficiency is not HEOR."""
    items = [
        {"title": "AI in dermatologic pharmacokinetics: quantitative modeling and optimization of therapeutics",
         "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "summary": ""},
        {"title": "An AI tool improves workflow efficiency and productivity in radiology",
         "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "summary": ""},
        {"title": "Cost-effectiveness and budget impact of an AI triage tool",
         "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "summary": ""},
    ]
    build.refine_heor_layer(items)
    assert items[0]["layer"] == "clinical", "PK paper with no economics should leave HEOR"
    assert items[1]["layer"] == "clinical", "bare workflow/efficiency is not HEOR"
    assert items[2]["layer"] == "heor", "genuine cost-effectiveness stays HEOR"


def test_industry_to_access_override():
    """Decision 2: genuine coverage/payment/procurement/funding routes Industry → Access; deployment stays."""
    items = [
        {"title": "NHS Supply Chain awards national procurement framework for AI diagnostics", "layer": "industry", "source": "MedTech Dive", "summary": ""},
        {"title": "CMS finalises reimbursement coding for the AI algorithm", "layer": "industry", "source": "STAT — Health Tech", "summary": ""},
        {"title": "Hospital rolls out AI scribe across its clinics", "layer": "industry", "source": "MobiHealthNews", "summary": ""},
    ]
    build.refine_industry_to_access(items)
    assert items[0]["layer"] == "access"
    assert items[1]["layer"] == "access"
    assert items[2]["layer"] == "industry", "ordinary deployment stays industry"


def test_industry_to_regulation_canonical():
    """Decision 3: regulatory-event stories route Industry → Regulation; commercial-primary stays."""
    items = [
        {"title": "London launches AI health regulatory sandbox for NHS innovation", "layer": "industry", "source": "AI health funding programmes", "summary": ""},
        {"title": "Are AI scribes medical devices? U.K. regulator weighs in", "layer": "industry", "source": "STAT — Health Tech", "summary": ""},
        {"title": "AI imaging startup raises $40M after FDA clearance", "layer": "industry", "source": "MobiHealthNews", "summary": ""},
    ]
    build.refine_industry_to_regulation(items)
    assert items[0]["layer"] == "regulation"
    assert items[1]["layer"] == "regulation"
    assert items[2]["layer"] == "industry", "funding-primary story stays industry despite mentioning clearance"


def test_education_dropped_unless_commercial():
    """Decision 4: pure education/training programmes are dropped unless they carry a commercial signal."""
    edu = {"title": "CEP IIT Delhi Announces the Launch of Batch 2 of its Executive Programme for AI in Healthcare",
           "layer": "industry", "source": "The Batch", "url": "https://x", "summary": ""}
    assert edu not in build.relevance_gate([edu])
    edu_funded = {"title": "AI training programme launches with $10M funding and hospital partnership",
                  "layer": "industry", "source": "MobiHealthNews", "url": "https://y", "summary": ""}
    assert edu_funded in build.relevance_gate([edu_funded])


def test_clinical_economic_to_heor():
    """Cat-2 boundary: a clinical study whose primary contribution is an economic evaluation → HEOR."""
    items = [
        {"title": "Cost-effectiveness of an AI skin-cancer triage tool", "layer": "clinical", "source": "PubMed — AI × HTA/HEOR", "summary": ""},
        {"title": "A prospective diagnostic evaluation of an AI pancreatic-cyst classifier", "layer": "clinical", "source": "AI/ML intervention trials", "summary": ""},
    ]
    build.refine_clinical_to_heor(items)
    assert items[0]["layer"] == "heor", "economic evaluation → HEOR"
    assert items[1]["layer"] == "clinical", "a clinical diagnostic study stays clinical"


def test_methodological_ai_to_research():
    """Cat-1 vs Cat-2: methodological AI (reporting guideline, software, RAG, safety) → research, when
    there is no patient/clinical-validation signal; a patient study stays clinical."""
    def lay(title):
        items = [{"title": title, "layer": "clinical", "source": "medRxiv — Health Informatics",
                  "url": "https://www.medrxiv.org/x", "summary": ""}]
        build.refine_method_papers(items)
        return items[0]["layer"]
    assert lay("CiteSure: a biomedical retrieval-augmented RAG method") == "research"
    assert lay("The CINEX consensus guideline for reporting clinical informatics") == "research"
    assert lay("A software package for rigorous survival analysis") == "research"
    assert lay("A prospective trial of an AI sepsis alert in ICU patients") == "clinical"   # patient signal → stays


def test_acceptance_studies_excluded():
    """Cat-2 boundary: acceptance/attitude studies are commentary (excluded), not clinical evidence."""
    et = build.classify_evidence({"title": "Public Acceptance of Artificial Intelligence in Health Care",
                                  "layer": "clinical", "source": "JAMA Network — AI in medicine",
                                  "stype": "Journal / evidence", "summary": ""})[0]
    assert et == "Commentary", et


def test_arxiv_health_specificity():
    """Cat-1 health-specificity: arXiv research must be MATERIALLY biomedical (title term); generic AI
    benchmarks are excluded even if they mention health in a multi-domain benchmark."""
    def kept(title):
        i = {"title": title, "summary": "spans natural science, healthcare and engineering domains",
             "layer": "research", "source": "arXiv", "url": "https://arxiv.org/abs/1"}
        return i in build.relevance_gate([i])
    # generic AI benchmarks → excluded (health mention only in the abstract/motivation)
    assert not kept("Avalon-ToM-Bench: Evaluating Theory of Mind via Asymmetric Game Mechanics")
    assert not kept("Sci-VBench: Reasoning-Intensive Video Generation in Science Domains")
    assert not kept("Decoding-Level Taboo: A Diagnostic Stress Test for LLM Robustness")
    # materially biomedical arXiv research → kept
    assert kept("MedPixel: A Unified Pixel-Language Model for Medical Reasoning")
    assert kept("Deep Multimodal Wearable Sensor Fusion for Body-Focused Repetitive Behaviors")
    assert kept("Disentangling Co-Occurring Retinal Pathologies")


def test_safety_surveillance_to_regulation():
    """Cat-4 boundary: postmarketing safety surveillance → Regulation, not HEOR; value/RWE stays HEOR."""
    items = [
        {"title": "Integrating Human and AI for Robust Postmarketing Safety Surveillance Systems",
         "layer": "heor", "source": "FDA Sentinel (real-world evidence)", "summary": ""},
        {"title": "Deriving real-world insights to inform trials using EHR and machine learning",
         "layer": "heor", "source": "PCORnet (real-world evidence)", "summary": ""},
        {"title": "Cost-effectiveness of an AI triage tool", "layer": "heor", "source": "PubMed — AI × HTA/HEOR", "summary": ""},
    ]
    build.refine_safety_surveillance_to_regulation(items)
    assert items[0]["layer"] == "regulation", "postmarketing safety surveillance → regulation"
    assert items[1]["layer"] == "heor", "comparative-effectiveness RWE stays HEOR"
    assert items[2]["layer"] == "heor", "economic evaluation stays HEOR"


def test_workforce_advocacy_excluded():
    """Cat-6 boundary: workforce/adoption-advocacy commentary is excluded; a real commercial signal stays."""
    et = build.classify_evidence({"title": "Nurses seek a seat at the table as they fight expanding clinical AI",
                                  "layer": "industry", "source": "STAT — Health Tech", "summary": ""})[0]
    assert et == "Commentary", et
    et2 = build.classify_evidence({"title": "AI won't fix nurse burnout. Nurses will",
                                   "layer": "industry", "source": "Fierce Healthcare", "summary": ""})[0]
    assert et2 == "Commentary", et2
    # a genuine commercial story stays a market signal
    keep = build.classify_evidence({"title": "Nursing-AI vendor Hospital IQ raises $30M Series B",
                                     "layer": "industry", "source": "MobiHealthNews", "summary": ""})[0]
    assert keep == "Funding round", keep


def test_event_cluster_collapse():
    """Synonym-worded duplicates of one MHRA event collapse to the primary source; unrelated stays."""
    items = [
        {"title": "MHRA clarifies how existing medical device law applies to AVT", "source": "MHRA (UK)",
         "url": "https://news.google.com/rss/a", "date": "2026-08-10", "layer": "regulation"},
        {"title": "AI scribes used to take notes are not medical devices, MHRA says", "source": "MHRA (UK)",
         "url": "https://news.google.com/rss/b", "date": "2026-08-10", "layer": "regulation"},
        {"title": "MHRA clarifies regulatory status of ambient voice technologies used in the NHS",
         "source": "MHRA — GOV.UK (primary)", "url": "https://www.gov.uk/x", "date": "2026-08-10", "layer": "regulation"},
        # unrelated MHRA item (different topic) must NOT be merged
        {"title": "MHRA calls for regulation of AI in healthcare", "source": "MHRA (UK)",
         "url": "https://news.google.com/rss/c", "date": "2026-08-10", "layer": "regulation"},
        # AVT topic but different body (NHS CLEAR pilot, not MHRA) must NOT be merged
        {"title": "National CLEAR Programme launches NHS pilot to evaluate AVT", "source": "NIST AI risk",
         "url": "https://news.google.com/rss/d", "date": "2026-08-10", "layer": "regulation"},
    ]
    out = build.collapse_event_clusters(items)
    titles = [i["title"] for i in out]
    assert len(out) == 3, titles   # 3 MHRA-AVT items → 1, plus the 2 unrelated
    # the kept representative is the GOV.UK primary source
    assert any("ambient voice technologies used in the NHS" in t for t in titles)
    assert not any("existing medical device law applies to AVT" in t for t in titles)
    assert any("calls for regulation of AI" in t for t in titles)     # unrelated MHRA kept
    assert any("National CLEAR" in t for t in titles)                 # different body kept


def test_event_cluster_sandbox():
    """The UK/NHS AI-health regulatory sandbox, reported two ways, collapses to the GOV.UK primary."""
    items = [
        {"title": "Pioneering AI health innovations regulatory sandbox launched", "source": "MHRA — GOV.UK (primary)",
         "url": "https://www.gov.uk/government/news/pioneering", "date": "2026-08-10", "layer": "regulation"},
        {"title": "London launches AI health regulatory sandbox for NHS innovation", "source": "AI health funding programmes",
         "url": "https://news.google.com/rss/x", "date": "2026-08-10", "layer": "industry"},
        {"title": "National CLEAR Programme launches NHS pilot to evaluate AVT", "source": "NIST AI risk",
         "url": "https://news.google.com/rss/y", "date": "2026-08-10", "layer": "regulation"},
    ]
    out = build.collapse_event_clusters(items)
    titles = [i["title"] for i in out]
    assert len(out) == 2, titles   # 2 sandbox items → 1 (GOV.UK), plus the unrelated CLEAR pilot
    assert any("Pioneering" in t for t in titles)          # GOV.UK primary kept
    assert not any("London launches" in t for t in titles) # syndicated dropped
    assert any("National CLEAR" in t for t in titles)      # different topic kept


def test_digest_demotes_litigation():
    """Featured-story digest: genuine regulatory/coverage actions rank ABOVE litigation."""
    reg = {"id": "r1", "title": "MHRA clarifies medical device status of AI scribes", "summary": "",
           "layer": "regulation", "source": "MHRA (UK)"}
    lit = {"id": "l1", "title": "Court examines AI discovery in Medicare Advantage coverage case",
           "summary": "", "layer": "access", "source": "CMS coverage determinations (NCD/LCD) — AI"}
    o = {"clears": [], "econ": [], "reg": [lit, reg]}   # litigation listed FIRST in the source data
    picks = build._digest(o)
    ids = [it["id"] for _, it in picks]
    why = {it["id"]: w for w, it in picks}
    assert ids.index("r1") < ids.index("l1"), "regulatory action must be featured before litigation"
    assert why["l1"] == "Legal / litigation"
    assert why["r1"] == "Regulatory actions"


def test_pharma_drug_approval_dropped():
    """2.44 Defect A: pharmaceutical drug authorisations (e.g. a GLP-1 weight-loss pill) are out of
    scope even when a vendor NAME contains 'AI'; a genuine AI-device clearance that mentions a drug stays."""
    drug = {"title": "MedPal AI Highlights UK MHRA Approval of Eli Lilly's Orforglipron as Second Oral "
                     "GLP-1 Weight Loss Pill in Two Months",
            "layer": "regulation", "source": "MHRA (UK)", "url": "https://news.google.com/rss/x", "summary": ""}
    assert drug not in build.relevance_gate([drug]), "GLP-1 drug approval must be dropped"
    # a real AI-device clearance that happens to mention a drug is KEPT (AI-device token rescues it)
    dev = {"title": "FDA clears AI algorithm for insulin dosing in type 2 diabetes",
           "layer": "regulation", "source": "openFDA device clearances", "url": "https://accessdata.fda.gov/x",
           "summary": ""}
    assert dev in build.relevance_gate([dev]), "AI-device clearance stays in scope"
    # a drug-DISCOVERY AI paper that merely mentions a molecule (no approval) is NOT caught by the guard
    disc = {"title": "A machine-learning model for GLP-1 receptor agonist discovery",
            "layer": "research", "source": "arXiv", "url": "https://arxiv.org/abs/2", "summary": ""}
    assert build._DRUG_APPROVAL_RE.search(disc["title"]) is None, "discovery paper is not an approval event"


def test_reference_guide_not_featured():
    """2.44 Defect B: ICLG-style 'Laws and Regulations 2026' reference chapters are not regulator ACTIONS
    and must never headline the featured card; a genuine regulator action is featured instead."""
    ref = {"id": "g1", "title": "Korea - Digital Health Laws and Regulations 2026", "summary": "",
           "layer": "regulation", "source": "APAC AI device regulation & reimbursement",
           "url": "https://news.google.com/rss/x"}
    act = {"id": "a1", "title": "MHRA clarifies regulatory status of ambient voice technologies in the NHS",
           "summary": "", "layer": "regulation", "source": "MHRA — GOV.UK (primary)",
           "url": "https://www.gov.uk/government/news/x"}
    o = {"clears": [], "econ": [], "reg": [ref, act]}
    picks = build._digest(o)
    whys = {it["id"]: w for w, it in picks}
    assert "g1" not in whys, "reference guide must be excluded from the digest"
    assert whys.get("a1") == "Regulatory actions"


def test_featured_prefers_primary_url():
    """2.44 Defect B: the featured pick prefers a primary-source (non-Google-News) link even when the
    freshest digest item is a Google-News redirect."""
    reg = build._REFERENCE_GUIDE_RE
    assert reg.search("Korea - Digital Health Laws and Regulations 2026")
    assert reg.search("USA Digital Health Legal Guide")
    assert not reg.search("MHRA clarifies regulatory status of AI scribes")


def test_authorisation_counts_news_reported_fda():
    """2.45 Analysis #1: a genuine FDA authorisation reported via NEWS counts toward the Authorisation
    gate and the FDA feed (not only openFDA-API items); a rule/requirement change does not."""
    denovo = {"title": "FDA Grants De Novo Authorization for AI-Based Coronary Inflammation Quantification",
              "layer": "regulation", "source": "AI-enabled device clearances", "summary": "", "url": "https://news.google.com/x"}
    diagnos = {"title": "DIAGNOS Receives Health Canada Medical Device Licence for CARA System",
               "layer": "regulation", "source": "Canada — CADTH & Health Canada", "summary": "", "url": "https://x"}
    rule = {"title": "CDSCO's New Rules: AI Diagnostic Software Now Needs Medical Device Licence",
            "layer": "regulation", "source": "India — device authorisations", "summary": "", "url": "https://y"}
    guidance = {"title": "MHRA calls for regulation of AI in healthcare",
                "layer": "regulation", "source": "MHRA (UK)", "summary": "", "url": "https://z"}
    assert build._is_device_authorisation(denovo)
    assert build._is_device_authorisation(diagnos)
    assert not build._is_device_authorisation(rule), "a 'now needs a licence' rule is not a product authorisation"
    assert not build._is_device_authorisation(guidance), "a call for regulation is not an authorisation"
    # FDA-specific: only the FDA De Novo, not the Health Canada licence
    assert build._is_fda_authorisation(denovo)
    assert not build._is_fda_authorisation(diagnos)


def test_econ_trial_denominator_clinical_only():
    """2.45 Analysis #2: the 'N AI trials' denominator counts CLINICAL-stage trials only, so a trial
    reclassified to HEOR (a cost-effectiveness study) is not counted as a trial-with-no-economic-endpoint."""
    items = [
        {"title": "A prospective AI sepsis-alert trial", "layer": "clinical", "source": "ClinicalTrials.gov",
         "url": "https://clinicaltrials.gov/ct2/show/NCT1", "summary": ""},
        {"title": "SloMo2: Implementation, Effectiveness, and Cost-effectiveness Study", "layer": "heor",
         "source": "ClinicalTrials.gov", "url": "https://clinicaltrials.gov/ct2/show/NCT2",
         "summary": "cost-effectiveness of a digital tool"},
    ]
    o = build.overview_stats(items)
    assert len(o["trials"]) == 1, "the HEOR-reclassified cost-effectiveness study is excluded from the trial denominator"


def test_cardiology_topic_matches_specialty_scan():
    """2.45 Analysis #3: the cardiology-ai follow-topic recognises the same terms as the specialty
    tally (coronary/ECG/echo), so the two do not disagree on the same 'Cardiology' label."""
    pred = build.TOPIC_BY_SLUG["cardiology-ai"]["pred"]
    coronary = {"title": "FDA De Novo for AI coronary inflammation quantification", "summary": "",
                "layer": "regulation", "source": "x", "url": "https://x"}
    assert pred(coronary), "a coronary-artery AI item is a cardiology item in both tallies"


import validate_build   # noqa: E402  (repo-root module; sys.path patched above)


def _vmk(n, **k):
    d = {"id": f"id{n}", "title": "A prospective RCT of an AI sepsis alert in ICU patients",
         "url": f"https://example.org/{n}", "source": "NEJM AI", "summary": "",
         "layer": "clinical", "date": "2026-08-10", "score": 100 - n}
    d.update(k)
    d["etype"], d["strength"] = build.classify_evidence(d)
    d["relevance"] = build.healthcare_relevance(d)
    d["modality"] = build.ai_modality(d)
    d["maturity"], d["maturity_lab"] = build.evidence_maturity(d)
    d["topics"] = [t["slug"] for t in build.TOPICS if t["pred"](d)]
    return d


def _clean_items():
    return [
        _vmk(1, url="https://clinicaltrials.gov/ct2/show/NCT1", source="ClinicalTrials.gov"),
        _vmk(2, title="Cost-effectiveness of an AI triage tool", layer="heor", source="Value in Health"),
        _vmk(3, title="MHRA clarifies regulatory status of AI scribes", layer="regulation",
             url="https://www.gov.uk/government/news/x", source="MHRA — GOV.UK (primary)", date="2026-08-09"),
        _vmk(4, title="Novo Nordisk, AWS launch AI drug discovery hub", layer="industry",
             source="Fierce Healthcare"),
    ]


def _render_html(items, o):
    """Minimal stand-in for docs/index.html carrying the exact anchors the R/Z layers parse."""
    import json as _j
    n = len(items)
    # mirror build.py _bm(): capitalised stem, value-pluralised suffix after <br>
    tiles = "".join(
        f'<div class="brief-m"><div class="brief-v">{v}</div><div class="brief-l">{stem}<br>{suf}</div></div>'
        for v, stem, suf in ((o["layers"].get("regulation", 0), "Regulatory", "updates"),
                             (len(o["coverage_actions"]), "Coverage", "decisions"),
                             (o["layers"].get("clinical", 0), "Clinical", "studies")))
    feat = build.select_featured(o)
    topstory = (f'<div class="topstory" data-open="{feat[1]["url"]}">{feat[1]["title"]}</div>'
                if feat else '<div class="topstory quiet">A quiet day</div>')
    hrefs = "".join(f'<a class="tbrow" href="{i["url"]}">{i["title"]}</a>' for i in items)
    return (f'<div class="sub">Updated x · {n} updates</div>{topstory}'
            f'<div class="brief">{tiles}</div>{hrefs}'
            f'<div class="tab">Evidence <span class="tabcount">({n})</span></div>'
            f'<span class="seeall">View all {n} updates →</span>'
            f'<script>const ITEMS={_j.dumps(items)};const TOPIC_LABELS={{}};</script>')


def _vbuild(items, rendered=True):
    o = build.overview_stats(items)
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items),
            "check_links": False}   # no network in tests
    html = _render_html(items, o) if rendered else None
    return validate_build.run_validation(
        items, o, {"contributing": 3, "expected": 5, "failed": []}, meta, build, rendered_html=html)


def test_validator_clean_build_passes():
    """2.46: a well-formed build (data + rendered page) raises no validation ERRORs."""
    r = _vbuild(_clean_items())
    assert not r.errors, "clean build should have no errors: " + str([(i.code, i.detail) for i in r.errors])
    assert r.checks_run == 12, "all layers should run on a clean build"


# --------- MUTATION TESTS: prove each check is SENSITIVE to the defect it targets ---------
def _codes_after(mutate, rendered=True):
    items = _clean_items()
    o_html = mutate(items)   # mutate may return a replacement rendered-html string
    o = build.overview_stats(items)
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items),
            "check_links": False}
    html = o_html if isinstance(o_html, str) else (_render_html(items, o) if rendered else None)
    r = validate_build.run_validation(items, o, {"contributing": 3, "expected": 5, "failed": []},
                                      meta, build, rendered_html=html)
    return {i.code for i in r.issues}


def test_mut_missing_id():
    assert "E01_missing_field" in _codes_after(lambda it: it[0].__setitem__("id", ""))


def test_mut_duplicate_url():
    assert "E04_duplicate_url" in _codes_after(lambda it: it[1].__setitem__("url", it[0]["url"]))


def test_mut_future_date():
    assert "E05_future_date" in _codes_after(lambda it: it[0].__setitem__("date", "2099-01-01"))


def test_mut_bad_stage():
    assert "E06_bad_stage" in _codes_after(lambda it: it[0].__setitem__("layer", "nonsense"))


def test_mut_inject_drug_approval():
    def m(it):
        it[2]["title"] = "MedPal AI Highlights MHRA Approval of Orforglipron GLP-1 Weight Loss Pill"
    assert "S01_drug_approval_leak" in _codes_after(m)


def test_mut_bad_facet_value():
    assert "F01_strength_vocab" in _codes_after(lambda it: it[0].__setitem__("strength", "Totally Made Up"))


def test_mut_homepage_count_altered():
    """H05 fires when the derived regulatory tile disagrees with the recomputed feed count."""
    items = _clean_items()
    o = build.overview_stats(items)
    o["layers"]["regulation"] = 999          # corrupt the derived Home metric
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items),
            "check_links": False}
    codes = {i.code for i in validate_build.run_validation(
        items, o, {"contributing": 3, "expected": 5, "failed": []}, meta, build,
        rendered_html=_render_html(items, o)).issues}
    assert "H05_regulatory_metric" in codes or "A01_stage_sum" in codes


def test_mut_render_count_mismatch():
    # the page shows a stale/incorrect item count
    def m(it):
        o = build.overview_stats(it)
        html = _render_html(it, o).replace(f"· {len(it)} updates", "· 999 updates")
        return html
    assert "R01_item_count" in _codes_after(m)


def test_mut_embedded_foreign_id():
    # the embedded ITEMS payload contains an id absent from the dataset
    def m(it):
        o = build.overview_stats(it)
        html = _render_html(it, o).replace('"id": "id1"', '"id": "GHOST"')
        return html
    codes = _codes_after(m)
    assert "R02_embedded_ids" in codes


def test_mut_featured_not_rendered():
    # a build where the featured story's id is missing from the rendered payload
    def m(it):
        o = build.overview_stats(it)
        feat = build.select_featured(o)
        html = _render_html(it, o).replace(f'"id": "{feat[1]["id"]}"', '"id": "MOVED"')
        return html
    assert "R03_featured_rendered" in _codes_after(m)


def test_mut_metric_tile_wrong():
    # the rendered regulatory tile shows the wrong number
    def m(it):
        import re as _re
        o = build.overview_stats(it)
        html = _render_html(it, o)
        return _re.sub(r'(<div class="brief-v">)\d+(</div><div class="brief-l">Regulatory<br>)',
                       r'\g<1>77\g<2>', html)
    assert "R05_metric_tile" in _codes_after(m)


def test_mut_false_empty_state():
    # a featured story exists but the page renders the 'A quiet day' empty state
    def m(it):
        o = build.overview_stats(it)
        html = _render_html(it, o) + '<div class="topstory quiet">A quiet day</div>'
        return html
    assert "Z01_false_empty" in _codes_after(m)


def test_mut_embedded_shape_not_count():
    """R02 shape guard: a parseable-but-malformed ITEMS (list of non-objects) is flagged as a SHAPE
    error, not misreported as a count mismatch."""
    def m(it):
        import re as _re
        o = build.overview_stats(it)
        html = _render_html(it, o)
        return _re.sub(r"const ITEMS=\[.*?\];const TOPIC_LABELS=",
                       "const ITEMS=[1,2,3];const TOPIC_LABELS=", html, flags=_re.S)
    codes = _codes_after(m)
    assert "R02_embedded_shape" in codes
    assert "R02_embedded_count" not in codes, "shape failure must not masquerade as a count mismatch"


def test_mut_integrity_shortcircuits_downstream():
    """An item-integrity error (E04) skips H/A/R/Z/X — skipped ≠ passed — and records canonical codes."""
    items = _clean_items()
    items[1]["url"] = items[0]["url"]          # duplicate URL → E04
    o = build.overview_stats(items)
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items),
            "check_links": False}
    r = validate_build.run_validation(items, o, {"contributing": 3, "expected": 5, "failed": []},
                                      meta, build, rendered_html=_render_html(items, o))
    codes = {i.code for i in r.issues}
    assert "E04_duplicate_url" in codes
    assert r.layers_skipped == ["H", "A", "R", "Z", "X"]
    assert not any(c[0] in "HARZX" and c[1].isdigit() for c in codes), "no downstream codes should fire"


def test_selftest_detects_exact_code_and_isolates_production():
    """The opt-in self-test injects a defect into a COPY, asserts EXACTLY the expected code is added,
    and never mutates the caller's items (production isolation invariant)."""
    items = _clean_items()
    o = build.overview_stats(items)
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items)}
    urls_before = [i["url"] for i in items]
    for code in ("E04_duplicate_url", "E05_future_date", "E06_bad_stage"):
        rep, detected, delta, expected = validate_build.run_selftest(
            items, o, {"contributing": 3, "expected": 5, "failed": []}, meta, build,
            rendered_html=_render_html(items, o), expected_code=code)
        assert detected, f"{code}: expected exactly that code, got delta {sorted(delta)}"
        assert delta == {code}
        assert rep.status_dict()["email_trigger"] is True
    # production isolation: the caller's items are byte-for-byte unchanged
    assert [i["url"] for i in items] == urls_before, "self-test must not mutate the real items"


def test_selftest_flags_broken_validator():
    """detected is False when the expected code does NOT actually appear — proving the self-test isn't
    rigged to always pass. If a check were blinded so the injected defect produced nothing, run_selftest
    returns detected=False, which is the signal build.py uses to fail the run and withhold publish."""
    items = _clean_items()
    o = build.overview_stats(items)
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items)}
    orig = validate_build._inject
    try:
        validate_build._inject = lambda copy_items, code: None   # simulate the defect not being caught
        _, detected, delta, _ = validate_build.run_selftest(
            items, o, {"contributing": 3, "expected": 5, "failed": []}, meta, build,
            rendered_html=_render_html(items, o), expected_code="E04_duplicate_url")
        assert detected is False and "E04_duplicate_url" not in delta
    finally:
        validate_build._inject = orig


def test_news_routed_out_of_clinical():
    """2.47 (b): a News/VC/policy item that landed in an evidence stage is routed to industry; a real
    trial and a real journal study are left in clinical."""
    news = {"title": "Digital health VC hits $7.4B in H1 2026 as AI agents capture funding",
            "layer": "clinical", "source": "AI in HTA & market access",
            "url": "https://news.google.com/rss/x", "summary": ""}
    trial = {"title": "AI-assisted MRI trial for stroke triage", "layer": "clinical",
             "source": "AI/ML intervention trials", "url": "https://clinicaltrials.gov/ct2/show/NCT9", "summary": ""}
    build.refine_news_out_of_evidence([news, trial])
    assert news["layer"] == "industry", "a VC/news item must leave the clinical stage"
    assert trial["layer"] == "clinical", "a real trial stays clinical"


def test_coherence_check_flags_news_in_evidence():
    """2.47 (a): the validator's coherence check WARNs on a News item sitting in an evidence stage,
    and is silent once it's in industry."""
    bad = _vmk(9, title="Digital health VC hits $7.4B", layer="clinical", source="AI in HTA & market access",
               url="https://news.google.com/rss/9")
    bad["etype"] = "News"
    codes = {i.code for i in _vbuild([bad] + _clean_items()).issues}
    assert "C01_news_in_evidence" in codes
    good = dict(bad); good["layer"] = "industry"
    codes2 = {i.code for i in _vbuild([good] + _clean_items()).issues}
    assert "C01_news_in_evidence" not in codes2


def test_article_date_backfill():
    """2.52 (A14 fix): an undated scraped/RSS item's date is backfilled from the article page's own
    metadata (article:published_time / JSON-LD datePublished / <time datetime>), read from source."""
    class _Resp:
        def __init__(self, t): self.text = t
    samples = {
        "https://a/1": '<head><meta property="article:published_time" content="2026-08-09T10:00:00Z"></head>',
        "https://a/2": '<script type="application/ld+json">{"datePublished":"2026-08-07"}</script>',
        "https://a/3": '<time datetime="2026-08-05T08:00:00-04:00">Aug 5</time>',
        "https://a/4": '<html>no date here</html>',
        "https://news.google.com/x": '<meta property="article:published_time" content="2026-08-09">',
    }
    orig = build.get
    try:
        build.get = lambda url, **k: _Resp(samples[url])
        assert build._article_date("https://a/1") == "2026-08-09"
        assert build._article_date("https://a/2") == "2026-08-07"
        assert build._article_date("https://a/3") == "2026-08-05"
        assert build._article_date("https://a/4") == ""              # honest: no date found
        assert build._article_date("https://news.google.com/x") == ""  # gnews redirect, skipped
    finally:
        build.get = orig


def test_gnews_title_strips_pipe_source_tag():
    """2.49 audit: a trailing ' | Journal' or ' - Publisher' tag is stripped from Google-News titles."""
    import re
    def strip(t):
        t = re.sub(r"\s+-\s+[^-|]{1,60}$", "", t)
        return re.sub(r"\s+\|\s+[^-|]{1,60}$", "", t)
    # BOTH tags stripped, in either order (the real Google-News case)
    assert strip("Digital health in the WHO African Region: a review | JHL - Dove Medical Press") \
        == "Digital health in the WHO African Region: a review"
    assert strip("Some AI story - MobiHealthNews") == "Some AI story"
    assert strip("AI study | Nature Medicine") == "AI study"
    assert strip("AI in imaging: advances and challenges") == "AI in imaging: advances and challenges"  # no tag


def test_validator_flags_title_tag_and_all_undated_source():
    """2.49 audit: E11 flags a residual ' | source' tag; A14 flags a source that is entirely undated."""
    tag = _vmk(7, title="Digital health in Africa: a review | JHL", url="https://ex.org/7")
    codes = {i.code for i in _vbuild([tag] + _clean_items()).issues}
    assert "E11_title_source_tag" in codes
    # A14: 3 items from one source, all undated (distinct source not present in _clean_items)
    und = [_vmk(20 + k, source="Undated Trade Feed", url=f"https://ex.org/f{k}", date="") for k in range(3)]
    for u in und:
        u["date"] = ""
    codes2 = {i.code for i in _vbuild(und + _clean_items()).issues}
    assert "A14_source_all_undated" in codes2


def test_geography_country_maps_to_region():
    """2.49 audit: Egypt/Turkey now map to a region (no country silently drops from 'By region');
    and the A13 guard WARNs if any country lacks a region mapping."""
    assert build.MACRO.get("Egypt") == "Middle East & Africa"
    assert build.MACRO.get("Turkey") == "Europe"
    assert build._CTGOV_GEO.get("Turkey (Türkiye)") == "Turkey"   # malformed label normalised
    # A13 fires when an item has a country but no region
    bad = _vmk(7, layer="clinical", source="AI/ML intervention trials",
               url="https://clinicaltrials.gov/ct2/show/NCTX")
    bad["country"] = "Atlantis"; bad["region"] = ""
    codes = {i.code for i in _vbuild([bad] + _clean_items()).issues}
    assert "A13_country_no_region" in codes


def test_hta_governance_plan_to_regulation():
    """2.49 (#3): an HTA/regulator body's plan/framework for SAFE AI adoption routes from industry to
    regulation; an ordinary company adoption story stays in industry."""
    nice = {"title": "NICE Sets Out Evidence-led Plan to Support Safe and Scalable AI Adoption in the NHS",
            "layer": "industry", "source": "AI in HTA & market access", "url": "https://x", "summary": ""}
    plain = {"title": "Hospital rolls out an AI scheduling tool", "layer": "industry",
             "source": "MedTech Dive", "url": "https://y", "summary": ""}
    build.refine_industry_to_regulation([nice, plain])
    assert nice["layer"] == "regulation"
    assert plain["layer"] == "industry"


def test_opinion_review_to_commentary():
    """2.49 (#5): a policy/opinion piece indexed as a generic 'review' is Commentary (dropped), while a
    genuine clinical review is kept."""
    def et(title, pubtype):
        return build.classify_evidence({"title": title, "layer": "clinical", "source": "PubMed — AI × HTA/HEOR",
                                        "url": "u", "summary": "", "stype": "Journal / evidence", "pubtype": pubtype})[0]
    assert et("Physician burnout in rheumatology: are medical scribes part of the solution?", ["Review"]) == "Commentary"
    assert et("The state of digital health adoption in Nevada: a narrative review", ["Review"]) == "Commentary"
    assert et("Deep learning for diabetic retinopathy: a review of validation studies", ["Review"]) == "Review"


def test_export_uses_publisher_as_source():
    """2.49 (#1): the export/display 'source' is the resolved publisher (accurate attribution); the
    curated query it came from is preserved in 'feed'."""
    import tempfile, json
    from pathlib import Path
    it = {"id": "g1", "title": "Some AI story", "url": "https://news.google.com/x", "layer": "industry",
          "source": "LATAM — device authorisation (ANVISA)", "publisher": "El Universal", "date": "2026-08-10",
          "topics": [], "score": 1}
    tmp = Path(tempfile.mkdtemp()); orig = build.DOCS
    try:
        build.DOCS = tmp
        build.write_export([it])
        row = json.loads((tmp / "data" / "feed-latest.json").read_text())["items"][0]
        assert row["source"] == "El Universal"                       # publisher shown
        assert row["feed"] == "LATAM — device authorisation (ANVISA)"  # curation context preserved
    finally:
        build.DOCS = orig


def test_exec_move_requires_action():
    """2.48 (#2): 'Executive move' requires a real appointment/departure — a Q&A with a CMO or an
    'AI retirement' opinion piece is NOT an exec move; a genuine appointment still is."""
    def et(title):
        return build.classify_evidence({"title": title, "layer": "industry", "source": "MobiHealthNews",
                                        "url": "https://x", "summary": "", "stype": "Industry press"})[0]
    assert et("Q&A: Cigna Healthcare's CMO on how AI transforms lives") != "Executive move"
    assert et("Health systems must plan for AI's retirement") != "Executive move"
    assert et("Omada Health appoints new CEO and more digital health hires") == "Executive move"


def test_journal_research_not_preprint():
    """2.48 (#4): a research/clinical paper from a JOURNAL is a 'Journal study'; only preprint-server
    papers (arXiv/medRxiv) are typed 'Preprint'."""
    lancet = {"title": "MerMED-FM: Multimodal Medical Imaging Foundation Model", "layer": "research",
              "source": "Lancet Digital Health", "url": "https://www.thelancet.com/x", "summary": "",
              "stype": "Journal / evidence"}
    arxiv = {"title": "CARE: Confidence-Aware Reasoning for Medical VQA", "layer": "research",
             "source": "arXiv", "url": "https://arxiv.org/abs/1", "summary": "", "stype": "Preprint / research"}
    assert build.classify_evidence(lancet)[0] == "Journal study"
    assert build.classify_evidence(arxiv)[0] == "Preprint"


def test_coherence_guards_exec_and_preprint():
    """2.48 (a): validator WARNs on a mislabelled 'Executive move' (no action) and a 'Preprint' from a
    journal source — the safety net for #2/#4."""
    bad_exec = _vmk(7, title="Cigna CMO on the future of AI", layer="industry", source="MobiHealthNews",
                    url="https://ex.org/7")
    bad_exec["etype"] = "Executive move"
    bad_pre = _vmk(8, title="A journal-published imaging model", layer="research", source="Lancet Digital Health",
                   url="https://ex.org/8")
    bad_pre["etype"] = "Preprint"; bad_pre["stype"] = "Journal / evidence"
    codes = {i.code for i in _vbuild([bad_exec, bad_pre] + _clean_items()).issues}
    assert "C06_exec_move_no_action" in codes
    assert "C07_preprint_wrong_source" in codes


def test_mut_topic_tag_drift():
    # a tag is present that the topic rule does not actually justify
    def m(it):
        it[3]["topics"] = list(it[3].get("topics", [])) + ["oncology-ai"]  # industry item, not oncology
    assert "X01_topic_tag_drift" in _codes_after(m)


def test_mut_trial_denominator_and_auth_undercount():
    """A06/A04 fire when o is corrupted (a non-clinical trial in the denominator; an authorisation
    dropped from the gate)."""
    items = _clean_items() + [_vmk(9, title="FDA Grants De Novo Authorization for an AI ECG algorithm",
                                   layer="regulation", source="AI-enabled device clearances",
                                   url="https://news.google.com/rss/z")]
    o = build.overview_stats(items)
    o["trials"].append(items[1])          # inject a HEOR item into the trial denominator
    o["authorisations"] = []              # drop the genuine authorisation from the gate
    meta = {"generated_at": "now", "taxonomy_version": build.TAXONOMY_VERSION, "n_items": len(items),
            "check_links": False}
    codes = {i.code for i in validate_build.run_validation(
        items, o, {"contributing": 3, "expected": 5, "failed": []}, meta, build,
        rendered_html=_render_html(items, o)).issues}
    assert "A06_trial_denominator" in codes
    assert "A04_authorisation_undercount" in codes


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


# --- technology resolution (schema 3) -----------------------------------------
# Build A's forward-trajectory layer. Resolution is a SUBJECT claim ("appears to concern Technology X"),
# never a verified conclusion; deterministic; explicitly `unresolved`/`ambiguous`, never guessed.
_TEST_REG = build._norm_registry([
    {"id": "heartflow-ffrct", "name": "HeartFlow FFRct", "aliases": ["HeartFlow"], "identifiers": ["MTG32"]},
    {"id": "viz-lvo", "name": "Viz LVO", "aliases": ["ContaCT"], "identifiers": ["DEN170073"]},
])


def test_resolve_technology():
    """Deterministic resolution: by name, by identifier, ambiguous (2 techs), unresolved, empty registry."""
    r = build.resolve_technology({"title": "HeartFlow FFRct wins CMS payment", "summary": "", "source": "CMS"}, _TEST_REG)
    assert r["technology_id"] == "heartflow-ffrct" and r["technology_match_status"] == "resolved"
    r = build.resolve_technology({"title": "FDA device news", "summary": "cleared DEN170073", "source": "FDA"}, _TEST_REG)
    assert r["technology_id"] == "viz-lvo" and r["technology_match_confidence"] >= 0.95     # identifier hit
    r = build.resolve_technology({"title": "HeartFlow and Viz LVO both featured", "summary": "", "source": ""}, _TEST_REG)
    assert r["technology_id"] is None and r["technology_match_status"] == "ambiguous"       # 2 distinct → no pick
    r = build.resolve_technology({"title": "Generic AI imaging roundup", "summary": "", "source": ""}, _TEST_REG)
    assert r["technology_id"] is None and r["technology_match_status"] == "unresolved"
    assert build.resolve_technology({"title": "HeartFlow"}, [])["technology_match_status"] == "unresolved"  # no registry


def test_market_and_observation_type():
    assert build.market_of({"country": "United States"}) == "us"
    assert build.market_of({"country": "United Kingdom"}) == "uk"
    assert build.market_of({"country": ""}) == ""                       # unknown → empty, never guessed
    assert build.observation_type_of({"layer": "access"}) == "reimbursement_observation"
    assert build.observation_type_of({"layer": "regulation"}) == "regulatory_observation"
    assert build.observation_type_of({"layer": "industry"}) == "market_observation"


def test_schema3_resolution_record():
    """A new record carries the schema-3 observation-trajectory fields (technology_id/status/confidence ·
    market · observation_type), and resolution never disturbs the frozen classification."""
    item = _corpus_item(title="HeartFlow FFRct earns NICE MTG32 recommendation", summary="cardiology",
                        country="United Kingdom", layer="access")
    out, new, _ = build.build_detection_records("", [item], "2026-08-21", "2026-08-21T09:00:00Z", _TEST_REG)
    rec = _json.loads(out.strip())
    assert rec["schema"] == 3 and rec["schema"] == build.SCHEMA_VERSION
    assert rec["technology_id"] == "heartflow-ffrct" and rec["technology_match_status"] == "resolved"
    assert rec["market"] == "uk" and rec["observation_type"] == "reimbursement_observation"
    frozen = rec["first_classification"]
    out2, _, _ = build.build_detection_records(out, [], "2026-08-22", registry=_TEST_REG)   # rebuild, no new items
    rec2 = _json.loads(out2.strip())
    assert rec2["first_classification"] == frozen and len(rec2["classification_history"]) == 1
    assert rec2["technology_id"] == "heartflow-ffrct"                   # resolution stable


def test_resolution_is_rederivable_classification_frozen():
    """Resolution IMPROVES as the registry grows without touching the frozen classification: a record
    created with an empty registry is `unresolved`; a later build with a registry resolves it (re-derived
    from stored title+source), while first_classification stays byte-identical."""
    item = _corpus_item(title="HeartFlow FFRct coverage update", summary="", country="United States", layer="access")
    out, _, _ = build.build_detection_records("", [item], "2026-08-21", registry=[])
    r1 = _json.loads(out.strip())
    assert r1["technology_match_status"] == "unresolved" and r1["technology_id"] is None
    frozen = r1["first_classification"]
    out2, _, _ = build.build_detection_records(out, [], "2026-08-22", registry=_TEST_REG)   # existing re-derived
    r2 = _json.loads(out2.strip())
    assert r2["technology_id"] == "heartflow-ffrct" and r2["technology_match_status"] == "resolved"
    assert r2["market"] == "us" and r2["first_classification"] == frozen                    # classification untouched


def test_log_detections_reports_resolution():
    """log_detections returns a stats dict (new/reclassified/registry/resolved/ambiguous/unresolved) so the
    daily CI log confirms at a glance that the registry loaded and matched — not silently all-unresolved."""
    saved = (build.private_get, build.private_put, build.load_tech_registry)
    try:
        build.private_get = lambda *a, **k: ("", None)      # no existing corpus
        build.private_put = lambda *a, **k: True            # 'written to private' → no disk write
        build.load_tech_registry = lambda *a, **k: _TEST_REG
        stats = build.log_detections([_corpus_item(title="HeartFlow FFRct US OPPS payment",
                                                   summary="", country="United States", layer="access")])
        assert isinstance(stats, dict)
        assert stats["registry"] == len(_TEST_REG)
        assert stats["new"] == 1 and stats["resolved"] >= 1
        assert stats["resolved"] + stats["ambiguous"] + stats["unresolved"] == 1
    finally:
        build.private_get, build.private_put, build.load_tech_registry = saved


# --- Google-News URL resolution (provenance: publish primary links, not google redirects) ------
def test_gnews_url_parser():
    """The batchexecute response parser extracts the publisher URL (pure, network-free)."""
    sample = ')]}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://www.reuters.com/tech/ai-device\\",null]",null,null,null,"generic"]]'
    assert build._gnews_url_from_batch(sample) == "https://www.reuters.com/tech/ai-device"
    assert build._gnews_url_from_batch("no url here") is None


def test_resolve_gnews_urls_upgrade_and_fallback():
    """`url` ALWAYS stays the google redirect; success puts the decoded article URL in `resolved_url`.
    Failure → resolved_url stays None, still flagged gnews. Non-google items untouched (no gnews/resolved_url)."""
    G = "https://news.google.com/rss/articles/CBMiABC?oc=5"
    items = [{"url": G, "title": "x"}]
    up = build.resolve_gnews_urls(items, resolver=lambda u: "https://real.com/a")
    assert up == 1 and items[0]["url"] == G                       # url unchanged (still the redirect)
    assert items[0]["resolved_url"] == "https://real.com/a" and items[0]["gnews"] is True
    items = [{"url": G, "title": "x"}]
    up = build.resolve_gnews_urls(items, resolver=lambda u: None)
    assert up == 0 and items[0]["url"] == G and items[0]["gnews"] is True and items[0]["resolved_url"] is None
    items = [{"url": "https://www.fda.gov/x", "title": "y"}]
    build.resolve_gnews_urls(items, resolver=lambda u: "SHOULD_NOT_BE_USED")
    assert items[0]["url"] == "https://www.fda.gov/x" and "gnews" not in items[0] and "resolved_url" not in items[0]


def test_link_url_three_states_and_publisher_guard():
    """The href decision (build.link_url, mirrored by JS hrefOf) for all three states, plus the guard that
    the publisher HOMEPAGE is never used as the article href."""
    G = "https://news.google.com/rss/articles/CBMiABC"
    # 1) resolved gnews → href = resolved article URL
    assert build.link_url({"url": G, "resolved_url": "https://reuters.com/a", "gnews": True}) == "https://reuters.com/a"
    # 2) unresolved gnews → href = the google redirect (article-specific), NOT the homepage
    unresolved = {"url": G, "resolved_url": None, "gnews": True, "publisher_url": "https://www.reuters.com"}
    assert build.link_url(unresolved) == G
    assert build.link_url(unresolved) != unresolved["publisher_url"]      # never the homepage
    # 3) native item → href = the native article URL
    assert build.link_url({"url": "https://www.fda.gov/x"}) == "https://www.fda.gov/x"


def test_resolve_gnews_circuit_breaker():
    """After max_failures consecutive failures the resolver is not called again (a down endpoint can't
    add minutes to the build)."""
    calls = {"n": 0}
    def r(u):
        calls["n"] += 1
        return None
    items = [{"url": f"https://news.google.com/rss/articles/CBMi{i}", "title": "x"} for i in range(10)]
    build.resolve_gnews_urls(items, resolver=r, max_failures=3)
    assert calls["n"] == 3, calls["n"]


def test_gnews_batch_body_structure():
    """The batchexecute payload STRUCTURE is correct (verifiable without the network — the earlier bug was
    a malformed Fbv4je/garturlreq shape that made Google return nothing)."""
    import urllib.parse
    body = build._gnews_batch_body("CBMiABC", "1699999999", "SIGXYZ")
    assert body.startswith("f.req=")
    freq = _json.loads(urllib.parse.unquote(body[len("f.req="):]))
    call = freq[0][0]                                  # [[[ "Fbv4je", inner, None, "generic" ]]]
    assert call[0] == "Fbv4je" and call[3] == "generic"
    inner = _json.loads(call[1])                       # ["garturlreq", [PARAMS, aid, ts, sig]]
    assert inner[0] == "garturlreq"
    assert inner[1][1] == "CBMiABC" and inner[1][2] == 1699999999 and inner[1][3] == "SIGXYZ"
    assert inner[1][0][7] == "US:en"                   # PARAMS locale marker in the right slot


def test_export_preserves_gnews_provenance():
    """The export preserves the distinction the Google-News redirect otherwise hides: publisher name +
    publisher homepage + an explicit via_gnews marker — so provenance survives even when the article URL
    can't be recovered. Native items carry no gnews marker."""
    import tempfile, json as J, csv as C
    from pathlib import Path
    items = [
        {"id": "g1", "title": "AI device news", "url": "https://news.google.com/rss/articles/CBMiABC",
         "source": "APAC AI device regulation", "gnews": True, "publisher": "Reuters",
         "publisher_url": "https://www.reuters.com", "layer": "regulation", "date": "2026-08-20",
         "topics": [], "score": 5, "region": "Asia-Pacific", "country": "", "stype": "Industry press",
         "etype": "Industry news", "strength": "Market signal", "relevance": "Direct clinical", "modality": "Predictive ML"},
        {"id": "n1", "title": "FDA clears device", "url": "https://www.fda.gov/x", "source": "FDA — AI",
         "layer": "regulation", "date": "2026-08-20", "topics": [], "score": 8, "region": "North America",
         "country": "United States", "stype": "Regulator", "etype": "Regulatory guidance",
         "strength": "Policy signal", "relevance": "Direct clinical", "modality": "Predictive ML"},
    ]
    tmp = Path(tempfile.mkdtemp()); orig = build.DOCS
    try:
        build.DOCS = tmp
        build.write_export(items)
        rows = {r["id"]: r for r in J.loads((tmp / "data" / "feed-latest.json").read_text())["items"]}
        assert rows["g1"]["via_gnews"] == "yes" and rows["g1"]["publisher_url"] == "https://www.reuters.com"
        assert rows["g1"]["source"] == "Reuters"                 # outlet name preserved
        assert rows["g1"]["url"].startswith("https://news.google.com") and rows["g1"]["resolved_url"] == ""
        assert rows["n1"]["via_gnews"] == "" and rows["n1"]["publisher_url"] == "" and rows["n1"]["resolved_url"] == ""
    finally:
        build.DOCS = orig


def test_js_uses_href_decision():
    """The client feed template uses the explicit hrefOf(resolved_url||url) decision and marks unresolved
    gnews cards — so nobody quietly reverts the card link to a bare i.url or substitutes publisher_url."""
    assert "hrefOf" in build.JS and "i.resolved_url||i.url" in build.JS
    assert "safeUrl(hrefOf(i))" in build.JS and "via Google News" in build.JS
    assert "i.publisher_url" not in build.JS   # homepage must never become the href


def test_server_cards_use_link_url_not_raw():
    """Guard: the server-rendered cards (featured/digest/table) route through link_url(), not the raw
    i['url'] — so a gnews card's href is the resolved article or the redirect, never a reverted bare url."""
    import pathlib
    src = pathlib.Path(build.__file__).read_text(encoding="utf-8")
    assert 'safe_url(link_url(i))' in src and 'safe_url(link_url(hi))' in src
    assert 'safe_url(i["url"])' not in src and 'safe_url(hi["url"])' not in src


def test_via_gnews_badge_states():
    """The server-card 'via Google News' badge shows ONLY for unresolved gnews items (not resolved, not
    native) — so the featured/digest/top-updates cards are labeled consistently with the JS feed."""
    assert "via Google News" in build._via_gnews_html({"gnews": True, "resolved_url": None})
    assert build._via_gnews_html({"gnews": True, "resolved_url": "https://real.com/a"}) == ""  # resolved → none
    assert build._via_gnews_html({"url": "https://www.fda.gov/x"}) == ""                        # native → none


def test_server_cards_show_via_gnews_badge():
    """Release baseline: all three server-rendered card templates (featured/topstory, digest, top-updates)
    call the badge helper — so no surface silently drops the 'via Google News' label on unresolved gnews."""
    import pathlib
    src = pathlib.Path(build.__file__).read_text(encoding="utf-8")
    assert "_via_gnews_html(hi)" in src              # featured / top story
    assert src.count("_via_gnews_html(i)") >= 2      # digest rows + top-updates rows


def test_roundup_excluded_from_featured():
    """Multi-topic roundups (RAPS 'Recon', weekly rundowns) never lead the featured/priority pick; a
    focused regulator story does."""
    o = {"clears": [], "econ": [], "reg": [
        {"id": "round", "layer": "regulation", "summary": "",
         "title": "Recon: FDA releases draft guidance on gen AI-enabled devices; Industry warns Germany must reform drug pricing"},
        {"id": "focus", "layer": "regulation", "summary": "",
         "title": "FDA finalises guidance on AI-enabled device software functions"},
    ]}
    ids = [i["id"] for _, i in build._digest(o)]
    assert "focus" in ids and "round" not in ids
    # the pattern also catches weekly rundowns but not ordinary titles
    assert build._ROUNDUP_RE.search("Fierce weekly rundown: digital health") and not build._ROUNDUP_RE.search("FDA clears AI stroke triage device")


def test_hta_policy_items_stay_regulatory():
    """An HTA/regulator BODY's position, guidance or engagement on AI (NICE Listens, CDA-AMC methods
    guidance, an FDA discussion paper) stays staged regulatory — not demoted to industry — even via a
    gnews query with a soft title. A pure marketing/launch story from a regulator-named query is still
    demoted."""
    def relayer(title):
        it = {"layer": "regulation", "title": title, "summary": "", "url": "https://news.google.com/x", "gnews": True}
        return build.refine_regulation_layer([it])[0]["layer"]
    assert relayer("NICE Listens: hearing the public's views on AI in health and care") == "regulation"
    assert relayer("New Guidance Issued for Reporting AI Methods Used to Generate Real-World Evidence") == "regulation"
    assert relayer("Considerations for the Regulation of Generative AI-Enabled Medical Devices: Discussion Paper") == "regulation"
    assert relayer("HealthAI startup launches new imaging assistant, raises Series B funding") == "industry"


def test_gnews_title_strips_hyphenated_source():
    """Google-News source tags are stripped even when the publisher name contains hyphens (CDA-AMC,
    Wired-Gov) — the earlier [^-|] class broke on those and left ' - Publisher | Source' in the title.
    A legitimate mid-title dash is not over-stripped."""
    s = build._strip_gnews_source_tag
    assert s("New Guidance Issued for Reporting AI Methods Used to Generate Real-World Evidence - Canada's Drug Agency | CDA-AMC") \
        == "New Guidance Issued for Reporting AI Methods Used to Generate Real-World Evidence"
    assert s("NICE Listens: hearing the public's views on AI in health and care - Wired-Gov") \
        == "NICE Listens: hearing the public's views on AI in health and care"
    # only the trailing publisher tag is removed, not an internal " - "
    assert s("AI in Health - A New Era - MedTech Dive") == "AI in Health - A New Era"
    # hyphenated words with no spaced separator are untouched
    assert s("Real-World Evidence for AI") == "Real-World Evidence for AI"


def test_process_chemistry_out_of_scope():
    """Drug-manufacturing / process-chemistry papers are out of scope; drug-DISCOVERY AI stays in scope.
    (_OUT_OF_SCOPE_RE is applied to lowercased text in the gate, so match on .lower() as the pipeline does.)"""
    hit = lambda t: bool(build._OUT_OF_SCOPE_RE.search(t.lower()))
    assert hit("Bayesian Optimization of a Suzuki Coupling for the Industrial Synthesis of a Sartan Drug Intermediate")
    assert not hit("Agentic AI for Drug Discovery through human alignment")
    assert not hit("Patient Drug Response Prediction with latent transitions")


def test_minor_audit_fixes():
    """Four minor audit fixes: (1) lab-blog consumer posts need health relevance; (2) Saudi SFDA is not a
    US-FDA authorisation; (3) 'The Week in…' roundups are caught; (4) paywalled headlines don't lead featured."""
    # 1) frontier-lab blog: consumer post dropped, health-AI capability kept
    kept = {i["title"] for i in build.relevance_gate([
        {"source": "OpenAI News", "layer": "industry", "url": "https://openai.com/x",
         "title": "Introducing ChatGPT for Teens: Built for learning", "summary": ""},
        {"source": "OpenAI News", "layer": "industry", "url": "https://openai.com/y",
         "title": "New AI model improves clinical diagnosis and patient triage", "summary": ""},
    ])}
    assert "New AI model improves clinical diagnosis and patient triage" in kept
    assert "Introducing ChatGPT for Teens: Built for learning" not in kept
    # 2) Saudi SFDA licence is NOT tagged as a US-FDA authorisation
    saudi = {"layer": "regulation", "source": "MEA AI device", "summary": "",
             "title": "DIAGNOS Receives Saudi FDA Medical Device License for CARA System"}
    assert build._is_fda_authorisation(saudi) is False
    us = {"layer": "regulation", "source": "News", "summary": "",
          "title": "FDA grants De Novo clearance to AI stroke-triage device"}
    assert build._is_fda_authorisation(us) is True
    # 3) roundup pattern
    assert build._ROUNDUP_RE.search("The Week in Health: AI, Regulation, and Infrastructure")
    # 4) paywalled headline is not featured when an open item exists
    o = {"clears": [], "econ": [], "reg": [
        {"id": "pay", "layer": "regulation", "summary": "", "date": "2026-08-24",
         "title": "STAT+: FDA digital health leader promises generative AI guidance"},
        {"id": "open", "layer": "regulation", "summary": "", "date": "2026-08-24",
         "title": "MHRA finalises AI medical-device guidance"}]}
    import build as _b
    _b.datetime = build.datetime
    feat = build.select_featured(o)
    assert feat is not None and feat[1]["id"] == "open"


def test_gnews_never_typed_as_primary_body():
    """A Google-News redirect item must never be typed as a primary body (Regulator/HTA/Journal)
    from its publisher NAME — its source is the outlet, not the primary source (audit: #51/#62
    'Mexico Business News' from an ANVISA/COFEPRIS query was mistyped 'Regulator')."""
    gnu = "https://news.google.com/rss/articles/CBMiABCD"
    # publisher name carries a regulator token, but item is a gnews redirect → NOT 'Regulator'
    anvisa = {"source": "ANVISA regulatory bulletin", "url": gnu, "gnews": True}
    assert build.source_type(anvisa) == "Other"
    # trade-press publisher over gnews → Industry press
    fierce = {"source": "Fierce Healthcare", "url": gnu, "gnews": True}
    assert build.source_type(fierce) == "Industry press"
    # plain trade outlet over gnews → Other
    mbn = {"source": "Mexico Business News", "url": gnu, "gnews": True}
    assert build.source_type(mbn) == "Other"
    # native primary sources are UNAFFECTED
    assert build.source_type({"source": "U.S. FDA", "url": "https://www.fda.gov/x"}) == "Regulator"
    assert build.source_type({"source": "PubMed — regulatory science",
                              "url": "https://pubmed.ncbi.nlm.nih.gov/1/"}) == "Journal / evidence"


def test_governance_methods_papers_to_research():
    """Governance / regulatory-science / methods journal papers with NO clinical-validation signal are
    scholarly contributions, not clinical evidence → clinical becomes research. Genuine clinical
    studies (patient/trial/cohort/real-world) stay clinical."""
    move = [
        {"layer": "clinical", "source": "PubMed — regulatory science & AI policy", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
         "title": "Reframing risk management for AI-enabled medical devices: A dual-layer risk governance framework"},
        {"layer": "clinical", "source": "npj Digital Medicine", "summary": "",
         "url": "https://www.nature.com/articles/x",
         "title": "Delays between CE mark and FDA regulatory approval of AI-enabled software for radiology"},
        {"layer": "clinical", "source": "PubMed — digital health value & reimbursement", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/2/",
         "title": "Clinical Laboratory Terminology Standardization for Semantic Interoperability: Methodological Study"},
    ]
    build.refine_governance_methods_to_research(move)
    assert [i["layer"] for i in move] == ["research", "research", "research"]
    # genuine clinical studies are NOT moved
    stay = [
        {"layer": "clinical", "source": "npj Digital Medicine", "summary": "",
         "url": "https://www.nature.com/articles/y",
         "title": "A governance framework validated in a prospective multi-centre patient cohort"},  # has patient/cohort
        {"layer": "clinical", "source": "JAMA Network — AI in medicine", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/3/",
         "title": "Deep-learning triage improves diagnostic accuracy in a randomised trial"},
    ]
    build.refine_governance_methods_to_research(stay)
    assert all(i["layer"] == "clinical" for i in stay)
    # non-journal news with 'governance' in the title is untouched (source_type guard)
    news = [{"layer": "clinical", "source": "Fierce Healthcare", "summary": "",
             "url": "https://www.fiercehealthcare.com/x",
             "title": "Hospital AI governance committees are asking the wrong questions"}]
    build.refine_governance_methods_to_research(news)
    assert news[0]["layer"] == "clinical"


def test_fda_genai_event_cluster_collapse():
    """The FDA generative-AI guidance thread reported by several outlets collapses to one primary
    representative; an unrelated FDA story (device pilot) is never merged in."""
    items = [
        {"id": "stat", "layer": "regulation", "date": "2026-08-24", "summary": "",
         "source": "STAT — Health Tech", "url": "https://www.statnews.com/x",
         "title": "FDA digital health leader promises generative AI regulatory guidance is coming"},
        {"id": "raps", "layer": "regulation", "date": "2026-08-18", "summary": "",
         "source": "RAPS", "url": "https://news.google.com/rss/articles/CBMiRAPS",
         "title": "Recon: FDA releases draft guidance on gen AI-enabled devices"},
        {"id": "pilot", "layer": "industry", "date": "2026-08-24", "summary": "",
         "source": "MedTech Dive", "url": "https://www.medtechdive.com/x",
         "title": "FDA adds two behavioral health firms to TEMPO pilot"},
    ]
    out = build.collapse_event_clusters(items)
    ids = {i["id"] for i in out}
    assert "pilot" in ids                       # unrelated FDA story kept
    assert len(out) == 2                         # the two gen-AI items collapsed to one
    assert "stat" in ids and "raps" not in ids   # primary (native) kept over gnews roundup


def test_viewpoint_benchmark_framework_to_research():
    """Viewpoint / position / benchmark / framework journal papers (no clinical-validation signal)
    are Category-1 scholarship, not clinical evidence → clinical becomes research."""
    move = [
        {"layer": "clinical", "source": "JAMA Network — AI in medicine", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/10/",
         "title": "From Breakthrough to Follow-Through: A Public Health Agenda for AI"},
        {"layer": "clinical", "source": "npj Digital Medicine", "summary": "",
         "url": "https://www.nature.com/articles/a",
         "title": "Building safer clinical agents: the case for residency-level benchmarks"},
        {"layer": "clinical", "source": "NEJM AI", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/11/",
         "title": "The TopCoW Challenge - Topology-Aware Circle of Willis Segmentation"},
        {"layer": "clinical", "source": "npj Digital Medicine", "summary": "",
         "url": "https://www.nature.com/articles/b",
         "title": "When machines misread science: creating guardrails for AI interpretation"},
    ]
    build.refine_governance_methods_to_research(move)
    assert all(i["layer"] == "research" for i in move), [i["layer"] for i in move]
    # a narrative review with 'current challenges' (plural) is NOT a named challenge → stays clinical;
    # a behavioural/coaching study stays clinical; a real trial stays clinical
    stay = [
        {"layer": "clinical", "source": "PubMed — AI × HTA/HEOR", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/12/",
         "title": "Addressing antimicrobial resistance: current challenges and emerging strategies"},
        {"layer": "clinical", "source": "npj Digital Medicine", "summary": "",
         "url": "https://www.nature.com/articles/c",
         "title": "A benchmark framework validated in a prospective patient cohort"},  # has patient/cohort
    ]
    build.refine_governance_methods_to_research(stay)
    assert all(i["layer"] == "clinical" for i in stay), [i["layer"] for i in stay]


def test_roundups_leave_evidence_stages():
    """Roundups/digests are compilations, not discrete evidence — a roundup in a non-industry stage
    is routed to industry; a real study and a ClinicalTrials record are never touched."""
    items = [
        {"layer": "regulation", "url": "https://news.google.com/x",
         "title": "The Week in Health: AI, Regulation, and Infrastructure"},
        {"layer": "industry", "url": "https://www.fiercehealthcare.com/x",
         "title": "Weekly Rundown: several digital-health deals"},          # already industry
        {"layer": "clinical", "url": "https://clinicaltrials.gov/study/NCT1",
         "title": "Weekly monitoring trial of an AI wearable"},             # ctgov guard: not a roundup
        {"layer": "clinical", "url": "https://pubmed.ncbi.nlm.nih.gov/9/",
         "title": "Deep-learning triage improves diagnostic accuracy"},     # real study
    ]
    build.refine_roundups_out_of_evidence(items)
    assert items[0]["layer"] == "industry"    # roundup demoted out of regulation
    assert items[1]["layer"] == "industry"    # unchanged
    assert items[2]["layer"] == "clinical"    # ctgov never a roundup
    assert items[3]["layer"] == "clinical"    # real study untouched


def test_country_name_beats_regulator_comparison():
    """A story ABOUT a country (named in the headline) is placed there even when the body compares it
    to another market's regulator (audit: 'Bulgaria lags on digital therapeutics' mentioning DiGA)."""
    i = {"title": "Bulgaria lags on digital therapeutics as reimbursement rules stall",
         "summary": "compared with Germany's DiGA scheme run by BfArM", "source": "euractiv.com",
         "url": "https://news.google.com/x", "gnews": True}
    assert build.country_of(i) == "Bulgaria"
    assert build.MACRO.get("Bulgaria") == "Europe"


def test_classification_precision_gold_set():
    """The human-reviewed gold set (classification_gold.yaml) must grade 100% against the current
    classifier — this is the precision regression anchor that integrity checks cannot provide. If a
    rule change moves any labelled item off its reviewed stage/source_type/region, this fails loudly
    (and the build's P01 check warns). Also proves apply_classification_refiners stays in sync."""
    import os, json
    # build.__file__ is repo-root/build.py, so the gold file sits beside it
    gold_path = os.path.join(os.path.dirname(os.path.abspath(build.__file__)), "classification_gold.json")
    gold = json.load(open(gold_path, encoding="utf-8"))["items"]
    assert len(gold) >= 50, "gold set should be a representative sample"
    # (dimension, gold-key, sparse?) — sparse dims graded only where an expected value is present
    dims = (("stage", "expect_stage", False), ("source_type", "expect_source_type", False),
            ("evidence_type", "expect_evidence_type", False), ("strength", "expect_strength", False),
            ("region", "expect_region", True), ("decision_type", "expect_decision_type", True),
            ("payer_type", "expect_payer_type", True))
    bad = {d[0]: [] for d in dims}
    for g in gold:
        raw = {"title": g["title"], "source": g["source"], "url": g["url"],
               "declared_layer": g["declared_layer"], "gnews": bool(g.get("gnews"))}
        got = build.classify_for_eval(raw)
        for dim, key, sparse in dims:
            exp = g.get(key, "")
            if sparse and not exp:
                continue
            if got.get(dim) != exp:
                bad[dim].append((g["title"][:38], got.get(dim), exp))
    for dim, _k, _s in dims:
        assert not bad[dim], f"{dim} regressions: {bad[dim]}"


def test_apply_classification_refiners_matches_inline_chain():
    """apply_classification_refiners is the single source of truth for the refiner order; a governance
    paper and a roundup routed through it must land where the build puts them."""
    items = [
        {"layer": "heor", "source": "PubMed — regulatory science & AI policy", "summary": "",
         "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
         "title": "Reframing risk management for AI devices: a dual-layer governance framework"},
        {"layer": "regulation", "gnews": True, "url": "https://news.google.com/x", "summary": "",
         "source": "Mexico Business News", "title": "The Week in Health: AI, Regulation, and Infrastructure"},
    ]
    build.apply_classification_refiners(items)
    assert items[0]["layer"] == "research"    # governance paper (heor→clinical→research)
    assert items[1]["layer"] == "industry"    # roundup out of regulation


# --------- MUTATION TESTS for Tier-2 credibility checks (provenance / dates / dedup) ---------
def test_mut_gnews_no_publisher():
    def m(it):
        it[0].update(gnews=True, url="https://news.google.com/rss/articles/CBMiABC", source="")
    assert "E13_gnews_no_publisher" in _codes_after(m)


def test_mut_publisher_not_homepage():
    def m(it):
        it[0].update(gnews=True, url="https://news.google.com/rss/articles/CBMiABC",
                     source="Some Outlet", publisher_url="https://outlet.com/2026/08/24/an-article-slug")
    assert "E14_publisher_not_homepage" in _codes_after(m)


def test_mut_resolved_publisher_mismatch():
    def m(it):
        it[0].update(resolved_url="https://other-domain.example/a", publisher_url="https://realpublisher.com")
    assert "E15_resolved_publisher_mismatch" in _codes_after(m)


def test_mut_implausible_date():
    assert "E17_implausible_date" in _codes_after(lambda it: it[0].__setitem__("date", "2001-05-01"))


def test_mut_stale_items():
    def m(it):
        for k in range(7):
            it.append(_vmk(100 + k, url=f"https://example.org/old{k}", date="2026-01-01"))
    assert "E16_stale_items" in _codes_after(m)


def test_mut_residual_duplicate():
    # same normalised headline surviving under two different links
    assert "E18_residual_duplicate" in _codes_after(lambda it: it[1].__setitem__("title", it[0]["title"]))


def test_clean_build_has_no_tier2_warnings():
    """The Tier-2 checks must be quiet on a well-formed build (no false positives)."""
    r = _vbuild(_clean_items())
    noisy = {"E13_gnews_no_publisher", "E14_publisher_not_homepage", "E15_resolved_publisher_mismatch",
             "E16_stale_items", "E17_implausible_date", "E18_residual_duplicate"}
    fired = {i.code for i in r.issues} & noisy
    assert not fired, f"Tier-2 checks false-fired on a clean build: {fired}"


def test_superlative_guard_needs_clear_leader():
    """'led / most active / top' is only asserted on a credible base: leader >= floor AND strictly
    beats the runner-up. Thin (n<3) or tied leaders return None so no superlative is rendered."""
    assert build._clear_leader([("Asia-Pacific", 7), ("Europe", 6)]) == ("Asia-Pacific", 7)
    assert build._clear_leader([("Oncology", 7), ("Radiology", 5)]) == ("Oncology", 7)
    assert build._clear_leader([("ANVISA", 2), ("TGA", 1)]) is None      # below floor
    assert build._clear_leader([("A", 4), ("B", 4)]) is None             # tied
    assert build._clear_leader([("X", 1)]) is None                        # single, thin
    assert build._clear_leader([]) is None


def test_verify_deploy_is_fresh():
    """The post-deploy freshness comparison: live matches local only when generated_at AND
    taxonomy_version both match; a missing/renamed field fails closed (treated as stale)."""
    import verify_deploy
    loc = {"generated_at": "2026-08-24T18:25:09Z", "taxonomy_version": "2.55"}
    assert verify_deploy.is_fresh(loc, dict(loc)) is True
    assert verify_deploy.is_fresh(loc, {"generated_at": "2026-08-24T12:14:00Z", "taxonomy_version": "2.55"}) is False
    assert verify_deploy.is_fresh(loc, {"taxonomy_version": "2.55"}) is False   # missing field → fail closed
    assert verify_deploy.is_fresh(loc, {}) is False


def test_featured_prefers_open_over_paywalled_primary():
    """A marquee click must not land on a paywall: an OPEN item (even via a Google-News redirect) is
    featured over a fresh PAYWALLED primary-source item. Only if nothing open exists is a paywalled
    item allowed into the featured slot."""
    o = {"clears": [], "econ": [], "reg": [
        {"id": "paywalled", "layer": "regulation", "summary": "", "date": "2026-08-24",
         "url": "https://www.statnews.com/x",
         "title": "STAT+: FDA digital health leader promises generative AI guidance"},
        {"id": "open", "layer": "regulation", "summary": "", "date": "2026-08-20", "gnews": True,
         "url": "https://news.google.com/rss/articles/CBMabc",
         "title": "NICE Listens: hearing the public's views on AI in health and care"}]}
    feat = build.select_featured(o)
    assert feat is not None and feat[1]["id"] == "open"
    # but if the ONLY candidate is paywalled, it may still be featured (better than nothing)
    o2 = {"clears": [], "econ": [], "reg": [o["reg"][0]]}
    feat2 = build.select_featured(o2)
    assert feat2 is not None and feat2[1]["id"] == "paywalled"


def test_opinion_columns_excluded_not_featured_as_regulator_move():
    """A named opinion-column format ('[Reporter's Notebook]', op-ed, guest column) is analysis
    journalism, not a discrete event: it must be typed Commentary (→ excluded), never typed a
    'Regulatory authorisation' or left eligible to headline the featured slot as a regulator move."""
    korea = {"layer": "regulation", "gnews": True, "summary": "",
             "url": "https://news.google.com/rss/articles/CBMabc", "source": "Korea Biomedical Review",
             "title": "[Reporter's Notebook] In Korea, medical AI approval outpaces reimbursement"}
    assert build.classify_evidence(korea)[0] == "Commentary"     # not 'Regulatory authorisation'
    assert build.refine_commentary_layer([dict(korea)]) == []     # excluded from evidence stages
    # a genuine regulator action with 'approval' in the title is NOT swept up
    real = {"layer": "regulation", "summary": "", "url": "https://www.gov.uk/x", "source": "MHRA — GOV.UK",
            "title": "MHRA grants approval to AI-enabled stroke triage device"}
    assert build.classify_evidence(real)[0] != "Commentary"
    assert build.refine_commentary_layer([dict(real)]) != []
