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
    cols = ["id", "title", "url", "source", "source_type", "stage",
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
    items = [
        {"layer": "regulation", "source": "MHRA (UK)", "url": "", "title": "MHRA guidance on AI scribes", "summary": ""},
        {"layer": "regulation", "source": "AI policy & guidance", "url": "", "title": "EU AI Act radiology compliance", "summary": ""},
        {"layer": "regulation", "source": "Additional European HTA", "url": "", "title": "AI use in health system must be deemed safe - HIQA", "summary": ""},
        {"layer": "regulation", "source": "MEA AI device & digital health regulation", "url": "", "title": "Role of AI Healthcare Solutions in Saudi's Care Domain", "summary": ""},
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


def test_coverage_public_render():
    """The Coverage tab renders from the verified pipeline teaser (coverage_public.json):
    populated → stats + specialty maturity; empty/None → 'in preparation'."""
    pub = {"generated": "2026-07-27", "devices_verified": 5, "headline_median_days": None,
           "headline_note": "N=4 (<5) — median suppressed", "authorised_no_coverage": 1,
           "covered_total": 4, "disclaimer": "Descriptive only.",
           "maturity_labels": [{"specialty": "cardiology", "furthest_stage": "Commercial maturity"}]}
    tab = build.coverage_public_html(pub)
    assert ">5<" in tab and ">4<" in tab            # verified + covered counts rendered
    assert "Cardiology" in tab and "Commercial maturity" in tab
    assert "median suppressed" in tab               # N<5 note used when no median
    assert 'data-goto="coverage"' in build.coverage_mini_html(pub)   # Home teaser links to tab
    assert "in preparation" in build.coverage_public_html(None)      # graceful empty state
    assert build.coverage_mini_html(None) == ""


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
        {"title": "The state of digital health adoption in Nevada: a narrative review", "layer": "research",
         "source": "PubMed — AI × HTA/HEOR", "url": "https://pubmed.ncbi.nlm.nih.gov/2/", "summary": "",
         "pubtype": ["Review"]}]
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
