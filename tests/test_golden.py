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
