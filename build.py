#!/usr/bin/env python3
"""
Build a static AI x HEOR x Market Access feed page.

Fetches RSS feeds, the arXiv API, and a few non-RSS pages; asks Claude Haiku to
write a one-line "HEOR lens" for each new item; renders docs/index.html.

Run:  python build.py            (full build)
      python build.py --no-llm   (skip the lens pass; free, no API key needed)
"""

import argparse, hashlib, html, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser, requests, yaml
from bs4 import BeautifulSoup
try:
    from bs4 import MarkupResemblesLocatorWarning
    import warnings
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except Exception:
    pass

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
CACHE = ROOT / "data" / "cache.json"
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
LAYERS = ["research", "clinical", "heor", "regulation", "access", "industry"]
TIERS = ["daily", "weekly", "monthly"]

# Fetch helper with a fallback path for sources that reject the direct request.
PROXIES = [
    "https://api.allorigins.win/raw?url={}",
    "https://api.codetabs.com/v1/proxy?quest={}",
    "https://corsproxy.io/?{}",
]


def get(url, timeout=25):
    """Fetch a URL, with a fallback path on failure."""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as direct_err:
        quoted = requests.utils.quote(url, safe="")
        for p in PROXIES:
            try:
                r = requests.get(p.format(quoted), headers=UA, timeout=timeout + 20)
                r.raise_for_status()
                if not r.content.strip():
                    continue
                return r
            except Exception:
                continue
        raise direct_err   # original error is the informative one


# ------------------------------------------------------------- private store
# The public repo holds code and the rendered site. Everything that represents
# curation or accumulated work — the source list, the lens commentary cache, the
# trend history, the coverage dataset — lives in a PRIVATE repo and is pulled in
# at build time. With no token, the build falls back to local files so you can
# still develop and test offline.

PRIVATE_REPO = os.environ.get("PRIVATE_REPO", "")  # set in workflow env


def _gh_headers(token, raw=True):
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}


def private_get(path, token):
    """Fetch a file from the private repo. Returns (text, sha) or (None, None)."""
    if not token:
        return None, None
    try:
        r = requests.get(f"https://api.github.com/repos/{PRIVATE_REPO}/contents/{path}",
                         headers=_gh_headers(token, raw=False), timeout=25)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        meta = r.json()
        import base64
        return base64.b64decode(meta["content"]).decode("utf-8"), meta["sha"]
    except Exception as e:
        print(f"! private_get {path}: {type(e).__name__}", file=sys.stderr)
        return None, None


def private_put(path, text, token, sha=None, msg=None):
    """Write a file back to the private repo. Needs Contents: read & write."""
    if not token:
        return False
    try:
        import base64
        body = {"message": msg or f"update {path}",
                "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(f"https://api.github.com/repos/{PRIVATE_REPO}/contents/{path}",
                         headers=_gh_headers(token, raw=False), json=body, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"! private_put {path}: {type(e).__name__}", file=sys.stderr)
        return False


# ----------------------------------------------------------------- utilities
def uid(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def safe_url(u: str) -> str:
    """Only allow http(s) links; block javascript:/data: and escape for attribute use."""
    u = (u or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return "#"
    return html.escape(u, quote=True)


def clean(text: str, limit: int = 320) -> str:
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def load_cache(token=None) -> tuple:
    """Lens commentary cache. Once the lens is on this file IS your commentary corpus,
    so it lives in the private store, not the public repo."""
    text, sha = private_get("cache.json", token)
    if text:
        try:
            return json.loads(text), sha
        except json.JSONDecodeError:
            pass
    if CACHE.exists():                       # local fallback for offline dev
        try:
            return json.loads(CACHE.read_text()), None
        except json.JSONDecodeError:
            pass
    return {}, None


def save_cache(cache: dict, token=None, sha=None) -> None:
    trimmed = dict(sorted(cache.items(), key=lambda kv: kv[1].get("seen", ""), reverse=True)[:1500])
    text = json.dumps(trimmed, indent=1)
    if not private_put("cache.json", text, token, sha, "cache"):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(text)               # no token → keep it local


# ------------------------------------------------------------------ fetching
def fetch_rss(sources, cutoff, cap):
    items, dead = [], []
    for s in sources:
        try:
            parsed = feedparser.parse(get(s["url"]).content)
        except requests.HTTPError as e:
            dead.append(f"{s['name']}: HTTP {e.response.status_code}")
            continue
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}")
            continue
        if not parsed.entries:
            dead.append(f"{s['name']}: no entries")
            continue

        kept = 0
        for e in parsed.entries:
            if kept >= cap:
                break
            st = e.get("published_parsed") or e.get("updated_parsed")
            when = datetime.fromtimestamp(time.mktime(st), tz=timezone.utc) if st else datetime.now(timezone.utc)
            if when < cutoff:
                continue
            link = e.get("link")
            title = clean(e.get("title", ""), 200)
            if not link or not title:
                continue
            items.append({
                "id": uid(link), "title": title, "url": link,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": when.strftime("%Y-%m-%d"),
                "summary": clean(e.get("summary", "")),
            })
            kept += 1
    return items, dead


def fetch_arxiv(cfg, cutoff, cap):
    cats = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    url = ("http://export.arxiv.org/api/query?"
           f"search_query={requests.utils.quote(cats)}"
           "&sortBy=submittedDate&sortOrder=descending&max_results=120")
    try:
        parsed = feedparser.parse(get(url, timeout=30).content)
    except Exception as e:
        return [], [f"arXiv: {type(e).__name__}"]

    terms = [t.lower() for t in cfg["boost_terms"]]
    scored = []
    for e in parsed.entries:
        st = e.get("published_parsed")
        when = datetime.fromtimestamp(time.mktime(st), tz=timezone.utc) if st else datetime.now(timezone.utc)
        if when < cutoff:
            continue
        title = clean(e.get("title", ""), 200)
        blob = (title + " " + e.get("summary", "")).lower()
        score = sum(1 for t in terms if t in blob)
        scored.append((score, {
            "id": uid(e.link), "title": title, "url": e.link,
            "source": "arXiv", "tier": "daily", "layer": "research",
            "date": when.strftime("%Y-%m-%d"),
            "summary": clean(e.get("summary", "")),
        }))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:cap]], []


def fetch_scrape(sources):
    items, dead = [], []
    for s in sources:
        try:
            soup = BeautifulSoup(get(s["url"]).text, "html.parser")
        except requests.HTTPError as e:
            dead.append(f"{s['name']}: HTTP {e.response.status_code}")
            continue
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}")
            continue

        seen = set()
        for a in soup.find_all("a", href=True):
            href, text = a["href"], clean(a.get_text(), 200)
            if s["match"] not in href or len(text) < 25:
                continue
            full = urljoin(s["url"], href)
            if full in seen:
                continue
            seen.add(full)
            items.append({
                "id": uid(full), "title": text, "url": full,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "summary": "",
            })
            if len(seen) >= 8:
                break
    return items, dead


def fetch_pubmed(sources, lookback):
    """Pull journal records from PubMed's E-utilities API."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    items, dead = [], []
    for s in sources:
        try:
            r = requests.get(f"{base}/esearch.fcgi", timeout=25, headers=UA, params={
                "db": "pubmed", "term": s["query"], "retmax": s.get("max", 8),
                "sort": "date", "datetype": "pdat", "reldate": lookback, "retmode": "json",
            })
            r.raise_for_status()
            pmids = r.json()["esearchresult"]["idlist"]
            if not pmids:
                continue
            time.sleep(0.4)   # NCBI asks for <= 3 requests/sec
            r = requests.get(f"{base}/esummary.fcgi", timeout=25, headers=UA, params={
                "db": "pubmed", "id": ",".join(pmids), "retmode": "json",
            })
            r.raise_for_status()
            res = r.json()["result"]
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}")
            continue

        for pmid in pmids:
            rec = res.get(pmid)
            if not rec:
                continue
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            raw = (rec.get("sortpubdate") or rec.get("pubdate") or "")[:10].replace("/", "-")
            date = raw if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            authors = ", ".join(a["name"] for a in rec.get("authors", [])[:4])
            items.append({
                "id": uid(url), "title": clean(rec.get("title", ""), 220), "url": url,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": date, "summary": clean(authors, 160),
            })
        time.sleep(0.4)
    return items, dead


def fetch_gnews(sources, cutoff, cap):
    """Read selected publishers and standing topic queries via Google News."""
    items, dead = [], []
    for s in sources:
        q = requests.utils.quote(s["query"])
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        try:
            parsed = feedparser.parse(get(url).content)
        except Exception as e:
            dead.append(f"{s['name']} (via Google News): {type(e).__name__}")
            continue

        kept = 0
        for e in parsed.entries:
            if kept >= cap:
                break
            st = e.get("published_parsed")
            when = datetime.fromtimestamp(time.mktime(st), tz=timezone.utc) if st else datetime.now(timezone.utc)
            if when < cutoff:
                continue
            title = clean(e.get("title", ""), 200)
            title = re.sub(r"\s+-\s+[^-]+$", "", title)   # strip the trailing " - Publisher"
            items.append({
                "id": uid(e.link), "title": title, "url": e.link,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": when.strftime("%Y-%m-%d"), "summary": "",
            })
            kept += 1
    return items, dead


def fetch_federal_register(sources, lookback):
    """FDA/CMS guidance and notices via the Federal Register API."""
    items, dead = [], []
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).strftime("%Y-%m-%d")
    for s in sources:
        try:
            r = requests.get("https://www.federalregister.gov/api/v1/documents.json",
                             headers=UA, timeout=25, params={
                                 "per_page": s.get("max", 10), "order": "newest",
                                 "conditions[agencies][]": s["agency"],
                                 "conditions[term]": s["term"],
                                 "conditions[publication_date][gte]": since,
                                 "fields[]": ["title", "html_url", "publication_date", "type", "abstract"],
                             })
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}")
            continue

        for d in results:
            items.append({
                "id": uid(d["html_url"]), "title": clean(d.get("title", ""), 220),
                "url": d["html_url"], "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": d.get("publication_date", "")[:10],
                "summary": clean(d.get("abstract") or d.get("type") or "", 240),
            })
    return items, dead


def fetch_openfda(cfg, lookback):
    """AI-enabled device authorisations via the openFDA device APIs.
    Note: 510(k) searches device_name, PMA uses trade_name; a 404 means no matches."""
    if not cfg:
        return [], []
    since = (datetime.now(timezone.utc) - timedelta(days=lookback * 3)).strftime("%Y%m%d")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    items, dead = [], []

    endpoints = [
        ("510k", "510(k)", "cleared", "device_name", "k_number",
         "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID="),
        ("pma", "PMA", "approved", "trade_name", "pma_number",
         "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm?num="),
    ]

    for ep, label, verb, name_field, id_field, base_link in endpoints:
        terms = " OR ".join(f'{name_field}:"{t}"' for t in cfg["terms"])
        query = f'decision_date:[{since} TO {today}] AND ({terms})'
        try:
            r = requests.get(f"https://api.fda.gov/device/{ep}.json", headers=UA, timeout=25,
                             params={"search": query, "limit": cfg.get("max", 15),
                                     "sort": "decision_date:desc"})
            if r.status_code == 404:          # openFDA's way of saying "no matches"
                continue
            r.raise_for_status()
            results = r.json().get("results", [])
        except requests.HTTPError as e:
            dead.append(f"openFDA {label}: HTTP {e.response.status_code}")
            continue
        except Exception as e:
            dead.append(f"openFDA {label}: {type(e).__name__}")
            continue

        for d in results:
            num = d.get(id_field, "")
            name = d.get(name_field) or d.get("device_name") or "(unnamed device)"
            who = d.get("applicant", "")
            raw = str(d.get("decision_date", ""))
            if "-" in raw:
                date = raw[:10]
            elif len(raw) == 8:
                date = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
            else:
                date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            link = f"{base_link}{num}"
            items.append({
                "id": uid(link), "title": f"{label} {verb}: {clean(name, 150)}",
                "url": link, "source": "FDA — AI device authorisations",
                "tier": "weekly", "layer": "regulation", "date": date,
                "summary": clean(" · ".join(x for x in [who, num] if x), 160),
            })
    return items, dead


def fetch_ctgov(sources, lookback):
    """ClinicalTrials.gov API v2 — evidence generation as a leading indicator.
    The endpoints are the tell: an economic or utilisation endpoint means someone is
    building a payer dossier, ~18 months before it lands on your desk."""
    items, dead = [], []
    for s in sources:
        try:
            r = requests.get("https://clinicaltrials.gov/api/v2/studies", headers=UA, timeout=30, params={
                "query.term": s["query"],
                "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{(datetime.now(timezone.utc) - timedelta(days=lookback)).strftime('%Y-%m-%d')},MAX]",
                "pageSize": s.get("max", 10),
                "sort": "LastUpdatePostDate:desc",
            })
            r.raise_for_status()
            studies = r.json().get("studies", [])
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}")
            continue

        for st in studies:
            p = st.get("protocolSection", {})
            nct = p.get("identificationModule", {}).get("nctId", "")
            if not nct:
                continue
            title = p.get("identificationModule", {}).get("briefTitle", "")
            sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
            phase = ", ".join(p.get("designModule", {}).get("phases", []) or [])
            status = p.get("statusModule", {}).get("overallStatus", "")
            when = p.get("statusModule", {}).get("lastUpdatePostDateStruct", {}).get("date", "")
            outcomes = p.get("outcomesModule", {}).get("primaryOutcomes", []) or []
            primary = outcomes[0].get("measure", "") if outcomes else ""
            bits = " · ".join(x for x in [sponsor, phase, status] if x)
            items.append({
                "id": uid(nct), "title": clean(title, 200),
                "url": f"https://clinicaltrials.gov/study/{nct}",
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": when[:10] if when else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "summary": clean(f"{bits} — primary endpoint: {primary}" if primary else bits, 220),
            })
    return items, dead


def log_history(items, terms, token=None):
    """One row per build: per-layer counts and counts for each tracked term.
    Stored in the private data repo."""
    path = ROOT / "data" / "history.json"
    text, sha = private_get("history.json", token)
    if text:
        try:
            hist = json.loads(text)
        except json.JSONDecodeError:
            hist = []
    else:
        try:
            hist = json.loads(path.read_text()) if path.exists() else []
        except json.JSONDecodeError:
            hist = []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = " ".join((i["title"] + " " + i.get("summary", "")).lower() for i in items)
    row = {
        "date": today,
        "total": len(items),
        "layers": {l: sum(1 for i in items if i["layer"] == l) for l in LAYERS},
        "terms": {t: blob.count(t.lower()) for t in terms},
    }
    hist = [h for h in hist if h.get("date") != today] + [row]   # one row per day, last write wins
    hist = hist[-400:]                                            # ~13 months
    text_out = json.dumps(hist, indent=1)
    if not private_put("history.json", text_out, token, sha, f"history {today}"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_out)
    return row, hist


# ----------------------------------------------------------------- HEOR lens
# The prompt is your analytical framing, so it is not kept in this public repo.
# Set LENS_PROMPT as a repository secret (or in the private store as lens_prompt.txt).
# Without it, the build uses the neutral fallback below.
LENS_FALLBACK = """For each numbered item, write ONE sentence (max 30 words) on its practical
relevance to health economics, evidence generation, or market access.
If an item has no plausible relevance, output exactly: SKIP
Return ONLY a JSON object mapping each item's number (as a string) to its sentence."""


def lens_prompt(token=None):
    p = os.environ.get("LENS_PROMPT")
    if p:
        return p
    text, _ = private_get("lens_prompt.txt", token)
    return text or LENS_FALLBACK



TAKE_SYSTEM = """You are the editor of a daily monitor read by health-economics and market-access professionals tracking AI in medicine. From the items below, write a short editor's take: name the single most important development for AI market access, HEOR or device reimbursement today, and the implication a busy reader would otherwise miss. Sober and concrete — no hype, no adjectives like 'exciting' or 'groundbreaking', no lists. Write 2 sentences, max 45 words. If nothing is genuinely significant, say exactly that in one plain sentence."""


def weekly_take(items, o, token=None):
    """One editorial line for the top of the Overview. Needs ANTHROPIC_API_KEY;
    returns '' (banner hidden) without it."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return ""
    try:
        import anthropic
    except ImportError:
        return ""
    picks = _digest(o)
    if not picks:
        ctx = "No device authorisations, economic-endpoint trials, or major regulatory actions today."
    else:
        ctx = "Top items today:\n" + "\n".join(
            f"- [{why}] {i['title']} ({i['source']})" for why, i in picks[:8])
    ctx += (f"\n\nGates: authorisations {len(o['clears'])}, coverage/payment {len(o['coverage_actions'])}, "
            f"economic-endpoint trials {len(o['econ'])}, HTA/value papers {len(o['papers'])}.")
    if o["pathways"]:
        ctx += "\nPathways mentioned: " + ", ".join(f"{l} ({n})" for l, n in o["pathways"][:4]) + "."
    try:
        client = anthropic.Anthropic(api_key=key)
        r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=200,
                                   system=TAKE_SYSTEM, messages=[{"role": "user", "content": ctx}])
        return r.content[0].text.strip()
    except Exception as e:
        print(f"! weekly take failed ({type(e).__name__})", file=sys.stderr)
        return ""


def add_lens(items, token=None, model="claude-haiku-4-5-20251001", batch=12):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("! ANTHROPIC_API_KEY not set — skipping lens pass", file=sys.stderr)
        return items
    try:
        import anthropic
    except ImportError:
        print("! anthropic SDK missing — skipping lens pass", file=sys.stderr)
        return items

    client = anthropic.Anthropic(api_key=key)
    todo = [i for i in items if not i.get("lens")]
    print(f"  lens pass: {len(todo)} new items")

    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        payload = "\n\n".join(
            f"{n}. [{i['source']}] {i['title']}\n{i['summary'][:240]}"
            for n, i in enumerate(chunk)
        )
        try:
            resp = client.messages.create(
                model=model, max_tokens=1600, system=lens_prompt(token),
                messages=[{"role": "user", "content": payload}],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            mapping = json.loads(raw)
        except Exception as e:
            print(f"! lens batch failed ({type(e).__name__}) — continuing", file=sys.stderr)
            continue
        for n, i in enumerate(chunk):
            line = mapping.get(str(n), "").strip()
            if line and line != "SKIP":
                i["lens"] = line
    return items


# ----------------------------------------------------------------- trends
# Reads data/history.json (written by log_history). Renders signal tiles, a
# volume sparkline, and rising/falling terms. No LLM, no CDN: term counts are
# deterministic and auditable — you can always see why something spiked.

# --------------------------------------------------------- overview analytics
MACRO = {
    "United States": "North America", "Canada": "North America",
    "United Kingdom": "Europe", "European Union": "Europe", "Germany": "Europe", "France": "Europe",
    "Japan": "Asia-Pacific", "China": "Asia-Pacific", "Australia": "Asia-Pacific",
    "South Korea": "Asia-Pacific", "India": "Asia-Pacific", "Singapore": "Asia-Pacific",
    "Thailand": "Asia-Pacific", "Canada": "North America",
    "Saudi Arabia": "Middle East & Africa", "United Arab Emirates": "Middle East & Africa",
    "Israel": "Middle East & Africa", "South Africa": "Middle East & Africa",
}

# body -> role. Regulators gate market authorisation; HTA/payers gate reimbursement;
# professional societies set standards but make no binding decisions.
BODY_ROLE = {
    # regulators (market authorisation)
    "FDA": "regulator", "EMA": "regulator", "MHRA": "regulator",
    "PMDA": "regulator", "NMPA": "regulator", "TGA": "regulator", "MFDS": "regulator",
    "HSA": "regulator", "CDSCO": "regulator", "SFDA": "regulator", "SAHPRA": "regulator",
    # HTA & payer bodies (coverage / assessment)
    "CMS": "payer", "NICE": "payer", "G-BA": "payer", "IQWIG": "payer", "HAS": "payer", "BfArM": "payer",
    "PBAC": "payer", "MSAC": "payer", "HIRA": "payer", "NECA": "payer", "Chuikyo": "payer",
    "HITAP": "payer", "ACE": "payer", "CADTH": "payer", "MOHAP": "regulator", "HITAP Thailand": "payer",
    # professional societies (no binding decisions)
    "ISPOR": "professional", "HTAi": "professional",
}

# bodies distinctive enough to match safely in free text (no source feed of their own).
# Ambiguous acronyms (HAS, NICE, CMS, FDA, ACE) are matched by SOURCE only, never text.
SAFE_TEXT_BODIES = {"PMDA", "NMPA", "TGA", "MFDS", "HSA", "CDSCO", "SFDA", "SAHPRA",
                    "PBAC", "MSAC", "HIRA", "NECA", "Chuikyo", "MHRA", "BfArM", "IQWIG",
                    "G-BA", "ISPOR", "HTAi", "HITAP", "CADTH", "MOHAP", "PBAC", "MSAC"}


def country_of(i):
    """Best-effort country/jurisdiction for a regulatory or reimbursement item."""
    src = i.get("source", "")
    if any(k in src for k in ("FDA", "CMS")):
        return "United States"
    if "NICE" in src:
        return "United Kingdom"
    if "EMA" in src:
        return "European Union"
    if "DiGA" in src:
        return "Germany"
    blob = (i.get("title", "") + " " + i.get("summary", "")).lower()
    checks = [
        ("United States", ["ntap", "medicare", "medicaid", "510(k)", "de novo", "u.s. food and drug"]),
        ("Germany", ["diga", "bfarm", "g-ba", "nub-"]),
        ("France", ["pecan", "cnedimts", "lppr", "haute autorite"]),
        ("Japan", ["pmda", "japan", "chuikyo", "mhlw"]),
        ("China", ["nmpa", "china"]),
        ("Australia", ["tga", "pbac", "msac", "australia"]),
        ("South Korea", ["mfds", "hira", "neca", "south korea", "korea"]),
        ("India", ["cdsco", "india"]),
        ("Singapore", ["hsa singapore", "singapore", "agency for care effectiveness"]),
        ("Thailand", ["hitap", "thailand", "thai fda"]),
        ("Canada", ["cadth", "health canada", "canada"]),
        ("Saudi Arabia", ["sfda", "saudi"]),
        ("South Africa", ["sahpra", "south africa"]),
        ("United Arab Emirates", ["uae", "united arab emirates", "mohap", "dubai health", "abu dhabi"]),
        ("Israel", ["israel", "israeli"]),
        ("United Kingdom", ["nice ", " nhs", "mhra", "ukca", "early value assessment"]),
        ("European Union", ["ema ", "ce mark", "ce-mark", "eudamed", "european commission", "eu ai act", "joint clinical assessment"]),
    ]
    for label, keys in checks:
        if any(k in blob for k in keys):
            return label
    return None


def _body_role_counts(items):
    """Count items by named body, split into regulators / payers / professional.
    Distinctive bodies (SAFE_TEXT_BODIES) are also matched in title/summary, so
    APAC/MEA bodies surfacing via standing queries are captured even without a
    dedicated source feed. One body per role per item, to avoid over-counting."""
    from collections import Counter
    out = {"regulator": Counter(), "payer": Counter(), "professional": Counter()}
    for i in items:
        src = i.get("source", "")
        text = (src + " " + i.get("title", "") + " " + i.get("summary", "")).lower()
        matched = set()
        for b, role in BODY_ROLE.items():
            if role in matched:
                continue
            if (b in src) or (b in SAFE_TEXT_BODIES and b.lower() in text):
                out[role][b] += 1
                matched.add(role)
    return {k: v.most_common() for k, v in out.items()}


def _econ_endpoint(i):
    econ = ("cost", "economic", "utilisation", "utilization", "budget", "resource",
            "length of stay", "quality-adjusted", "qaly", "cost-effective", "resource use")
    return "clinicaltrials" in i["url"] and any(w in i.get("summary", "").lower() for w in econ)


SPECIALTIES = [
    ("Radiology & imaging", ["radiolog", "imaging", "mammogra", "ct scan", " mri", "x-ray", "chest"]),
    ("Cardiology", ["cardio", "cardiac", "heart", "coronary", "ecg", "echocardiog", "arrhythmia"]),
    ("Oncology", ["oncolog", "cancer", "tumour", "tumor", "carcinoma", "malignan"]),
    ("Ophthalmology", ["ophthalmo", "retina", "diabetic retinopathy", "glaucoma", "fundus"]),
    ("Pathology", ["patholog", "histolog", "biopsy", "cytolog"]),
    ("Neurology", ["neurolog", "brain", "stroke", "alzheimer", "seizure", "epilep"]),
    ("Gastroenterology", ["gastro", "endoscop", "colonoscop", "adenoma", "polyp"]),
    ("Dermatology", ["dermatolog", "skin lesion", "melanoma"]),
    ("Mental health", ["mental health", "psychiatr", "depression", "anxiety", "cbt"]),
    ("Endocrine / diabetes", ["diabet", "endocrin", "glucose", "insulin"]),
    ("Pulmonology", ["pulmonar", "lung", "respirator", "copd"]),
]


def clinical_focus(items):
    blob = [(i.get("title", "") + " " + i.get("summary", "")).lower() for i in items]
    out = []
    for label, keys in SPECIALTIES:
        n = sum(1 for t in blob if any(k in t for k in keys))
        if n:
            out.append((label, n))
    out.sort(key=lambda x: -x[1])
    return out


def active_orgs(items):
    """Sponsors and applicants, from fields we already parse: ClinicalTrials.gov
    summaries begin 'Sponsor · Phase · …'; openFDA authorisations are 'Applicant · number'.
    Directional — name formatting varies across sources."""
    from collections import Counter
    c = Counter()
    for i in items:
        if not ("clinicaltrials" in i["url"] or i["source"].startswith("FDA — AI device")):
            continue
        name = (i.get("summary", "").split(" · ")[0] or "").split(" — ")[0].strip()
        if len(name) < 3 or name.lower() in ("unknown", "n/a"):
            continue
        c[name] += 1
    return c.most_common(8)


def overview_stats(items):
    """Everything the Overview tab needs, derived from today's items with a
    market-access lens. No LLM — all rules, all auditable."""
    reg = [i for i in items if i["layer"] in ("regulation", "access")]
    clears = [i for i in items if i["source"].startswith("FDA — AI device")]
    trials = [i for i in items if "clinicaltrials" in i["url"]]
    econ = [i for i in trials if _econ_endpoint(i)]
    papers = [i for i in items if i["source"].startswith("PubMed — AI")]

    # evidence vs access balance
    research = sum(1 for i in items if i["layer"] in ("research", "clinical", "heor"))
    access = sum(1 for i in items if i["layer"] in ("regulation", "access", "industry"))

    # reimbursement-pathway chatter — which access route is in the news
    PATHWAYS = [
        ("NTAP", ["ntap", "new technology add-on"]),
        ("CPT / coding", ["cpt code", "cpt category", "coding"]),
        ("DiGA", ["diga"]),
        ("PECAN / France", ["pecan"]),
        ("NICE EVA", ["early value assessment", "eva "]),
        ("LCD / MAC", ["local coverage", "lcd "]),
        ("Reimbursement (general)", ["reimburse", "coverage decision", "payer"]),
    ]
    blob = [(i.get("title", "") + " " + i.get("summary", "")).lower() for i in items]
    pathways = []
    for label, keys in PATHWAYS:
        n = sum(1 for t in blob if any(k in t for k in keys))
        if n:
            pathways.append((label, n))
    pathways.sort(key=lambda x: -x[1])

    layers = {k: sum(1 for i in items if i["layer"] == k) for k in LAYERS}
    # the two market-access gates, as concrete decisions
    coverage_actions = [i for i in items if i["layer"] == "access"
                        and any(k in i["source"] for k in ("CMS", "NICE", "Federal"))]

    # geography: country + macro-region (over regulatory/reimbursement items)
    from collections import Counter
    countries = Counter(c for c in (country_of(i) for i in reg) if c)
    macro = Counter(MACRO.get(c, "Other") for c in (country_of(i) for i in reg) if c)
    # bodies by role (over all items, so ISPOR/HTAi in any layer are caught)
    bodies = _body_role_counts(items)

    return {
        "reg": reg, "clears": clears, "trials": trials, "econ": econ, "papers": papers,
        "research": research, "access": access, "pathways": pathways,
        "layers": layers, "coverage_actions": coverage_actions,
        "focus": clinical_focus(items),
        "countries": countries.most_common(), "macro": macro.most_common(),
        "bodies": bodies,
    }


def _digest(o):
    """Highest-consequence items pulled to the top by rule."""
    picks, seen = [], set()

    def add(items, why):
        for i in items:
            if i["id"] in seen:
                continue
            seen.add(i["id"])
            picks.append((why, i))

    add(o["clears"], "Device authorisations")
    add(o["econ"], "Trials · economic endpoint")
    add([i for i in o["reg"] if any(b in i["source"] for b in ("FDA", "CMS", "EMA", "NICE"))],
        "Regulatory actions")
    return picks[:8]


def overview_html(items, agg, o, history=None, take=""):
    # ---- pipeline pulse: one cell per category, mirrors the Feed tabs
    prior = (history or [])[:-1]
    SHORT = {"research": "Research", "clinical": "Clinical", "heor": "HEOR",
             "regulation": "Regulatory", "access": "Reimbursement", "industry": "Industry"}
    def pdelta(k):
        base = [h["layers"][k] for h in prior[-7:] if k in h.get("layers", {})]
        if len(base) < 2:
            return ""
        avg = sum(base) / len(base)
        d = o["layers"][k] - avg
        if abs(d) < 1.5:
            return '<span class="pd flat">±0</span>'
        a, c = ("▲", "up") if d > 0 else ("▼", "down")
        return f'<span class="pd {c}">{a}{abs(d):.0f}</span>'
    pulse = "".join(
        f'<div class="pulse-c" data-goto="feed"><div class="pl">{SHORT[k]}</div>'
        f'<div class="pv">{o["layers"][k]} {pdelta(k)}</div></div>' for k in LAYERS)
    pulse_html = (f'<div class="sec">Activity by stage</div>'
                  f'<div class="seccap">Where today\u2019s {len(items)} items fall across the six stages an AI '
                  f'product moves through \u2014 from research to reimbursement. Arrows show the change vs the past week.</div>'
                  f'<div class="pulse">{pulse}</div>')

    # ---- the two market-access gates, then leading indicators
    def render_tiles(rows):
        return "".join(
            f'<div class="tile"><div class="tl">{t}</div><div class="tv">{v}</div>'
            f'<div class="ts">{sub if "&" in sub else html.escape(sub)}</div></div>' for t, v, sub in rows)
    gate_tiles = [
        ("Gate 1 · Can it be sold?", len(o["clears"]),
         "New AI device authorisations today, from the FDA openFDA database (510(k), PMA)."),
        ("Gate 2 · Will it be paid?", len(o["coverage_actions"]),
         "Payment decisions today from the bodies we track as feeds — CMS (US) and NICE (UK)."),
    ]
    ind_tiles = [
        ("Trials building a payer case", len(o["econ"]),
         f"AI trials (ClinicalTrials.gov) whose primary endpoint is economic, not accuracy — {len(o['econ'])} of {len(o['trials'])} today."),
        ("HTA &amp; value papers", len(o["papers"]),
         "Peer-reviewed health-economics studies on AI, from PubMed, today."),
    ]
    gate_html = render_tiles(gate_tiles)
    ind_html = render_tiles(ind_tiles)

    # must-not-miss digest
    picks = _digest(o)
    if picks:
        from collections import OrderedDict
        groups = OrderedDict()
        for why, i in picks:
            groups.setdefault(why, []).append(i)
        boxes = ""
        for why, gitems in groups.items():
            grows = "".join(
                f'<a class="dig" href="{safe_url(i["url"])}" target="_blank" rel="noopener">'
                f'<span class="dttl">{html.escape(i["title"])}</span>'
                f'<span class="dsrc">{html.escape(i["source"])} · {i["date"]}</span></a>'
                for i in gitems)
            boxes += (f'<div class="digbox"><div class="digbox-h">{why}'
                      f'<span class="digbox-n">{len(gitems)}</span></div>{grows}</div>')
        digest = f'<div class="sec">Worth a closer look</div><div class="seccap">The highest-consequence items today, pulled to the top by rule — new device authorisations, trials with an economic endpoint, and actions from a major regulator (FDA, CMS, EMA, NICE).</div><div class="digboxes">{boxes}</div>'
    else:
        digest = ('<div class="sec">Worth a closer look</div>'
                  '<div class="seccap">The highest-consequence items today, pulled to the top by rule — new device authorisations, trials with an economic endpoint, and actions from a major regulator (FDA, CMS, EMA, NICE).</div>'
                  '<div class="dnote">No device authorisations, economic-endpoint trials, or major '
                  'regulatory actions today. A quiet day.</div>')

    # --- shared bar-panel builder ---
    def bar_panel(title, sub, rows, empty, color="#9c2c2c"):
        if rows:
            peak = rows[0][1] or 1
            bars = "".join(
                f'<div class="trow"><div class="tn">{html.escape(str(lbl))}</div>'
                f'<div class="tb"><div class="tf" style="width:{n/peak*100:.0f}%;background:{color}"></div></div>'
                f'<div class="tp" style="color:{color}">{n}</div></div>' for lbl, n in rows[:6])
            return f'<div class="panel"><div class="ph">{title}</div><div class="psub">{sub}</div>{bars}</div>'
        return f'<div class="panel"><div class="ph">{title}</div><div class="psub">{empty}</div></div>'

    bodies = o.get("bodies", {})
    GEO_C = "#2f6f9f"
    def geo_rows(rows):
        if not rows:
            return '<div class="psub" style="margin-bottom:2px">none today</div>'
        peak = rows[0][1] or 1
        return "".join(
            f'<div class="trow"><div class="tn">{html.escape(str(lbl))}</div>'
            f'<div class="tb"><div class="tf" style="width:{n/peak*100:.0f}%;background:{GEO_C}"></div></div>'
            f'<div class="tp" style="color:{GEO_C}">{n}</div></div>' for lbl, n in rows[:6])
    geo_panel = (f'<div class="panel"><div class="ph">Geography</div>'
                 f'<div class="psub">market-access activity today</div>'
                 f'<div class="subh">By region</div>{geo_rows(o.get("macro", []))}'
                 f'<div class="subh" style="margin-top:9px">By country</div>{geo_rows(o.get("countries", []))}</div>')
    regulators_panel = bar_panel("Regulators", "market-authorisation bodies (FDA, EMA)",
                                 bodies.get("regulator", []), "No regulator activity today.", color="#6a4c93")
    payers_panel = bar_panel("HTA &amp; payer bodies", "coverage &amp; assessment (CMS, NICE)",
                             bodies.get("payer", []), "No HTA / payer activity today.", color="#1f8a70")
    clinfocus = bar_panel("Clinical focus", "therapeutic areas mentioned today",
                          o.get("focus", []), "No specialty clearly identified today.", color="#b5563a")
    pathway = bar_panel("Reimbursement pathways in the news", "items mentioning each route, today",
                        o.get("pathways", []), "None mentioned today.", color="#b0842b")
    prof_rows = bodies.get("professional", [])
    prof_panel = bar_panel("Professional bodies", "societies &amp; standards (ISPOR, HTAi)",
                           prof_rows, "", color="#64748b") if prof_rows else ""

    # compact coverage summary (full detail lives on the Coverage tab)
    cov_mini = ""
    if agg:
        cells = "".join(
            f'<div class="cmini"><div class="cm-l">{m["label"]}</div>'
            f'<div class="cm-v">{m["median"] if m["median"] is not None else "—"}'
            f'<span>{"d" if m["median"] is not None else ""}</span></div></div>'
            for _, m in agg["markets"].items())
        cov_mini = (f'<div class="sec">Clearance → coverage <a class="seeall" '
                    f'data-goto="coverage">full tracker →</a></div>'
                    f'<div class="cov-grid">{cells}</div>')

    # ---- "At a glance" hero: deterministic executive summary (no LLM needed) ----
    prior_h = (history or [])[:-1]
    hero_lines = []
    # biggest week-over-week mover (needs a little history)
    moves = []
    for k in LAYERS:
        base = [h["layers"][k] for h in prior_h[-7:] if k in h.get("layers", {})]
        if len(base) >= 2:
            moves.append((o["layers"][k] - sum(base) / len(base), k))
    if moves:
        moves.sort(key=lambda x: -abs(x[0]))
        d, k = moves[0]
        if abs(d) >= 1.5:
            hero_lines.append(f'<b>{SHORT[k]}</b> activity {"up" if d > 0 else "down"} '
                              f'{abs(d):.0f} vs last week — the day\'s biggest move')
    # single most consequential item
    hpicks = _digest(o)
    if hpicks:
        why, hi = hpicks[0]
        hero_lines.append(f'Most consequential: <a href="{safe_url(hi["url"])}" target="_blank" '
                          f'rel="noopener">{html.escape(hi["title"])}</a> <span class="hero-tag">{why.lower()}</span>')
    # most active market + body
    if o.get("macro"):
        reg = o["macro"][0]
        allb = o["bodies"]["regulator"] + o["bodies"]["payer"]
        tb = max(allb, key=lambda x: x[1]) if allb else None
        line = f'Most active market: <b>{html.escape(reg[0])}</b> ({reg[1]})'
        if tb:
            line += f' · leading body: <b>{html.escape(tb[0])}</b> ({tb[1]})'
        hero_lines.append(line)
    if hero_lines:
        hl = "".join(f'<div class="hero-line">{x}</div>' for x in hero_lines)
        hero = (f'<div class="hero"><div class="hero-h">At a glance '
                f'<span class="hero-n">{len(items)} items today</span></div>{hl}</div>')
    else:
        hero = ""

    pathway_row = (f'<div class="panels" style="margin-top:8px">{pathway}{prof_panel}</div>'
                   if prof_panel else f'<div style="margin-top:8px">{pathway}</div>')
    take_html = (f'<div class="take"><div class="take-l">Editor\'s take</div>'
                 f'<div class="take-t">{html.escape(take)}</div></div>') if take else ""
    return f'''{take_html}{hero}
{pulse_html}
<div class="sec">The two gates</div>
<div class="seccap">The two hurdles every AI product must clear, in order. Each tile counts today\u2019s items about that gate.</div>
<div class="tiles g2">{gate_html}</div>
<div class="sec">Leading indicators</div>
<div class="seccap">Evidence forming before a product reaches either gate — an economic trial endpoint signals a payer dossier in the making.</div>
<div class="tiles g2">{ind_html}</div>
{digest}
<div class="sec">The breakdown</div>
<div class="seccap">Today’s items by market, regulatory and HTA body, clinical area, and reimbursement route.</div>
<div class="panels">{geo_panel}{regulators_panel}</div>
<div class="panels" style="margin-top:8px">{payers_panel}{clinfocus}</div>
{pathway_row}
{cov_mini}'''


# --------------------------------------------------------- coverage tracker
COVERED = {"covered", "covered_provisional", "covered_early_access", "covered_regional"}
EV_DESIGN = ["rct", "prospective_obs", "retrospective", "modelling", "none", "unknown"]
EV_ENDPOINT = ["clinical_outcome", "diagnostic_accuracy", "economic", "workflow", "composite", "unknown"]
EV_COMPARATOR = ["standard_of_care", "no_ai", "placebo", "none", "unknown"]
EV_DESIGN_LABEL = {"rct": "RCT", "prospective_obs": "Prospective obs.",
                   "retrospective": "Retrospective", "modelling": "Modelling only",
                   "none": "No study", "unknown": "Unknown"}
EV_ENDPOINT_LABEL = {"clinical_outcome": "Clinical outcome", "diagnostic_accuracy": "Diagnostic accuracy",
                     "economic": "Economic", "workflow": "Workflow / time", "composite": "Composite",
                     "unknown": "Unknown"}
MARKETS = [("us", "United States"), ("de", "Germany"), ("fr", "France"), ("uk", "United Kingdom")]


def load_coverage():
    """Fetch coverage.yaml from the private repo. No token → no panel, no error."""
    token = os.environ.get("COVERAGE_TOKEN")
    if not token:
        print("  no COVERAGE_TOKEN — coverage panel omitted", file=sys.stderr)
        return None
    text, _ = private_get("coverage.yaml", token)
    if not text:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(f"! coverage.yaml is malformed ({e.__class__.__name__})", file=sys.stderr)
        return None


def _days(t0, t1):
    try:
        a = datetime.strptime(str(t0), "%Y-%m-%d")
        b = datetime.strptime(str(t1), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    d = (b - a).days
    return d if d >= 0 else None


def coverage_aggregates(data):
    """Medians and counts only. Device rows never leave this function."""
    if not data or not data.get("devices"):
        return None
    devices = data["devices"]
    out = {"n_devices": len(devices), "markets": {}, "statuses": {}, "fastest": None,
           "n_pccp": sum(1 for d in devices if d.get("pccp"))}

    for key, label in MARKETS:
        lags, statuses = [], []
        for d in devices:
            auth = (d.get("authorisation") or {})
            # US clocks from the FDA decision; EU markets clock from CE mark
            t0 = (auth.get("us") or {}).get("date") if key == "us" else (auth.get("eu") or {}).get("date")
            for c in (d.get("coverage") or {}).get(key, []) or []:
                statuses.append(c.get("status", "unknown"))
                if c.get("status") in COVERED:
                    lag = _days(t0, c.get("date"))
                    if lag is not None:
                        lags.append((lag, d.get("type", "other")))
        if lags:
            vals = sorted(l for l, _ in lags)
            mid = len(vals) // 2
            median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) // 2
            fastest = min(lags, key=lambda x: x[0])
            out["markets"][key] = {"label": label, "median": median, "n": len(vals),
                                   "fastest": fastest[0]}
            if out["fastest"] is None or fastest[0] < out["fastest"]["days"]:
                out["fastest"] = {"days": fastest[0], "market": label, "type": fastest[1]}
        else:
            out["markets"][key] = {"label": label, "median": None, "n": 0, "fastest": None}
        for st in statuses:
            out["statuses"][st] = out["statuses"].get(st, 0) + 1

    # evidence that won coverage — aggregate only, no row detail leaves this function
    ev = {"n": 0, "design": {}, "endpoint": {}, "comparator": {}, "accuracy_only": 0}
    for d in devices:
        for key, _ in MARKETS:
            for c in (d.get("coverage") or {}).get(key, []) or []:
                if c.get("status") not in COVERED:
                    continue
                e = c.get("evidence")
                if not isinstance(e, dict):
                    continue
                ev["n"] += 1
                dg = e.get("design", "unknown"); ep = e.get("endpoint", "unknown"); cp = e.get("comparator", "unknown")
                ev["design"][dg] = ev["design"].get(dg, 0) + 1
                ev["endpoint"][ep] = ev["endpoint"].get(ep, 0) + 1
                ev["comparator"][cp] = ev["comparator"].get(cp, 0) + 1
                if ep == "diagnostic_accuracy":
                    ev["accuracy_only"] += 1
    out["evidence"] = ev
    return out


def trends_html(items, history):
    """Trends TAB: volume sparkline + rising/falling terms. Tiles moved to Overview."""
    if not history:
        return '<div class="dnote">No history yet — the first build has just run.</div>'
    today = history[-1]
    prior = history[:-1]

    spark = ""
    if len(history) >= 3:
        vals = [h["total"] for h in history[-42:]]
        hi, lo = max(vals), min(vals)
        rng = (hi - lo) or 1
        w, ht = 300, 44
        step = w / max(len(vals) - 1, 1)
        pts = " ".join(f"{n*step:.1f},{ht - ((v-lo)/rng)*(ht-6) - 3:.1f}" for n, v in enumerate(vals))
        spark = (f'<div class="spark"><div class="ph">Volume</div>'
                 f'<svg viewBox="0 0 {w} {ht}" preserveAspectRatio="none">'
                 f'<polyline points="{pts}" fill="none" stroke="#9c2c2c" stroke-width="1.6" '
                 f'stroke-linejoin="round" opacity=".8"/></svg>'
                 f'<div class="sparl">Items per day · {len(vals)} days · low {lo} / high {hi}</div></div>')
    else:
        spark = (f'<div class="spark"><div class="ph">Volume</div>'
                 f'<div class="psub">A sparkline appears once there are 3+ builds on record.</div></div>')

    terms_html = ""
    if len(prior) >= 3:
        rows = []
        for term, now in today.get("terms", {}).items():
            base = [h["terms"].get(term, 0) for h in prior[-28:]]
            avg = sum(base) / len(base) if base else 0
            if now == 0 and avg < 0.5:
                continue
            pct = (100 if now else 0) if avg == 0 else ((now - avg) / avg) * 100
            rows.append((pct, term, now))
        rows.sort(key=lambda r: -r[0])
        top = (rows[:5] + [("sep",)] + rows[-2:]) if len(rows) > 7 else rows[:6]
        peak = max((abs(r[0]) for r in rows if len(r) == 3), default=1) or 1
        bars = ""
        for r in top:
            if len(r) == 1:
                bars += '<div class="tsep"></div>'; continue
            pct, term, now = r
            up = pct >= 0
            bars += (f'<div class="trow"><div class="tn{"" if up else " dim"}">{html.escape(term)}</div>'
                     f'<div class="tb"><div class="tf{"" if up else " down"}" style="width:{min(abs(pct)/peak*100,100):.0f}%"></div></div>'
                     f'<div class="tp{"" if up else " dim"}">{"+" if up else ""}{pct:.0f}%</div></div>')
        terms_html = (f'<div class="panel"><div class="ph">Rising &amp; falling terms</div>'
                      f'<div class="psub">today vs 28-day average</div>{bars}</div>')
    else:
        need = max(4 - len(history), 1)
        terms_html = (f'<div class="panel"><div class="ph">Rising &amp; falling terms</div>'
                      f'<div class="psub">Accruing — term trends need a few days of history. '
                      f'~{need} more to go.</div></div>')

    orgs = active_orgs(items)
    if orgs:
        peak = orgs[0][1] or 1
        bars = "".join(
            f'<div class="trow"><div class="tn" style="width:200px">{html.escape(n)}</div>'
            f'<div class="tb"><div class="tf" style="width:{k/peak*100:.0f}%"></div></div>'
            f'<div class="tp">{k}</div></div>' for n, k in orgs)
        orgs_html = (f'<div class="panel"><div class="ph">Most active organisations</div>'
                     f'<div class="psub">sponsors &amp; applicants across trials and clearances — directional</div>{bars}</div>')
    else:
        orgs_html = ('<div class="panel"><div class="ph">Most active organisations</div>'
                     '<div class="psub">No sponsors or applicants identified today.</div></div>')
    return f'<div class="panels">{spark}{terms_html}</div><div style="margin-top:8px">{orgs_html}</div>'




def _evidence_panel(ev):
    """Public evidence aggregate — 'what won coverage'. Percentages and mix only;
    no device, date or citation ever appears here."""
    if not ev or ev.get("n", 0) == 0:
        return ('<div class="cov" style="margin-top:10px"><div class="ph">Evidence that won coverage</div>'
                '<div class="psub" style="margin-top:4px">No evidence packages logged yet. Add an '
                '<code>evidence</code> block to covered decisions in coverage.yaml — see TAXONOMY.md.</div></div>')
    n = ev["n"]
    rct = ev["design"].get("rct", 0)
    rct_pct = round(rct / n * 100)
    def mixbars(dist, labels):
        rows = sorted(dist.items(), key=lambda x: -x[1])
        peak = rows[0][1] if rows else 1
        return "".join(
            f'<div class="trow"><div class="tn">{html.escape(labels.get(k, k))}</div>'
            f'<div class="tb"><div class="tf" style="width:{v/peak*100:.0f}%"></div></div>'
            f'<div class="tp">{v}</div></div>' for k, v in rows)
    acc = ev["accuracy_only"]
    return f'''<div class="cov" style="margin-top:10px">
  <div class="cov-head" style="margin-bottom:8px"><b>Evidence that won coverage</b> · {n} decision{'s' if n != 1 else ''} with a logged package</div>
  <div class="cov-foot" style="border:none;margin:0 0 10px;padding:0">
    <span><b>{rct_pct}%</b> backed by an RCT</span>
    <span><b>{acc}</b> won on diagnostic accuracy alone</span>
  </div>
  <div class="panels">
    <div class="panel"><div class="ph">Study design</div><div class="psub">of decisions with logged evidence</div>{mixbars(ev["design"], EV_DESIGN_LABEL)}</div>
    <div class="panel"><div class="ph">Winning endpoint</div><div class="psub">the argument that convinced the payer</div>{mixbars(ev["endpoint"], EV_ENDPOINT_LABEL)}</div>
  </div>
  <div class="cov-note">Aggregate of the private evidence library. Design and endpoint vocabularies defined in TAXONOMY.md.</div>
</div>'''

def coverage_html(agg, sample=False, draft=False):
    if draft:
        return ('<div class="dnote">The clearance-to-coverage tracker is in preparation and will '
                'appear here once the underlying data has been verified.</div>')
    if not agg:
        return ('<div class="dnote">The clearance-to-coverage tracker is in preparation and will '
                'appear here.</div>')
    banner = ('<div class="cov-sample">Sample data — these are illustrative placeholder rows, '
              'not real devices. Remove <code>sample: true</code> from coverage.yaml once real '
              'devices are logged.</div>') if sample else ''
    cols = "".join(
        f'''<div class="cov-cell"><div class="cov-mkt">{m["label"]}</div>
             <div class="cov-num">{m["median"] if m["median"] is not None else "—"}<span>{"d" if m["median"] is not None else ""}</span></div>
             <div class="cov-sub">{("n=" + str(m["n"])) if m["n"] else "no data yet"}</div></div>'''
        for _, m in agg["markets"].items())
    fast = agg["fastest"]
    fast_line = (f'Fastest observed route: <b>{fast["days"]}d</b> ({fast["market"]}, {fast["type"]})'
                 if fast else "")
    refused = sum(v for k, v in agg["statuses"].items() if k in ("refused", "withdrawn", "expired"))
    evidence_panel = _evidence_panel(agg.get("evidence"))
    return f'''{banner}
<div class="cov">
  <div class="cov-head">Median days from market authorisation to first obtainable reimbursement</div>
  <div class="cov-grid">{cols}</div>
  <div class="cov-foot">
    <span><b>{agg["n_devices"]}</b> devices tracked</span>
    <span><b>{agg["n_pccp"]}</b> with a PCCP</span>
    <span><b>{refused}</b> refused / withdrawn / expired</span>
    <span>{fast_line}</span>
  </div>
  <div class="cov-note">Aggregates only. Definitions in
    <a href="https://github.com/asarmah123/ai-heor-feed/blob/main/TAXONOMY.md" target="_blank" rel="noopener">TAXONOMY.md</a> —
    provisional, regional and code-only statuses are counted separately and never merged into a median.</div>
</div>
{evidence_panel}'''


# ------------------------------------------------------------------- render
CSS = """
:root{color-scheme:light;--line:#e8e8e8;--mute:#767676;--ink:#1a1a1a;--accent:#9c2c2c}
*{box-sizing:border-box}
body{margin:0;padding:26px 20px 60px;background:#fff;color:var(--ink);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:22px;margin:0;letter-spacing:-.015em;font-weight:650}
.tagline{font-size:13px;color:#5a5a5a;margin:2px 0}
.sub{color:var(--mute);font-size:12px;margin-bottom:16px}
/* tabs */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:20px;
 position:sticky;top:0;background:#fff;z-index:10;padding-top:2px}
.tab{font-size:13.5px;padding:8px 15px;color:#6a6a6a;cursor:pointer;border-bottom:2px solid transparent;border-radius:6px 6px 0 0;
 white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--ink);font-weight:650;border-bottom:2px solid var(--accent);background:#fbf6f6}
.view{display:none}.view.on{display:block}
.sec{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#5a5a5a;
 margin:26px 0 10px;display:flex;align-items:center;gap:10px}
.sec:first-child{margin-top:6px}
.seeall{font-size:10.5px;font-weight:600;letter-spacing:0;text-transform:none;color:var(--accent);
 cursor:pointer;text-decoration:none}
.seeall:hover{text-decoration:underline}
/* tiles */
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tiles.g2{grid-template-columns:repeat(2,1fr)}
.seccap{font-size:12.5px;color:#5f5f5f;margin:-4px 0 10px;line-height:1.45}
.tile{border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.tl{font-size:11px;color:#5a5a5a;text-transform:uppercase;letter-spacing:.05em}
.tv{font-size:22px;font-weight:650;margin-top:3px}
.ts{font-size:11px;color:#6f6f6f;margin-top:2px;line-height:1.35}
/* digest */
.digboxes{display:flex;flex-direction:column;gap:10px}
.digbox{border:1px solid var(--line);border-radius:9px;overflow:hidden}
.digbox-h{font-size:9.5px;font-weight:650;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:#fafafa;padding:7px 13px;border-bottom:1px solid #f0f0f0}
.digbox-n{color:#b3b3b3;margin-left:5px}
.dig{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:baseline;
 padding:10px 13px;text-decoration:none;color:var(--ink);border-bottom:1px solid #f4f4f4}
.dig:last-child{border-bottom:none}
.dig:hover{background:#fafafa}
.dttl{font-size:13px;font-weight:500;line-height:1.35}
.dsrc{font-size:11px;color:#767676;white-space:nowrap}
.dnote{border:1px dashed var(--line);border-radius:9px;padding:16px;font-size:12.5px;color:#8a8a8a}
/* panels */
.panels{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.panel,.spark{border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.ph{font-size:12.5px;font-weight:650}
.psub{font-size:11px;color:#6a6a6a;margin-bottom:9px}
.spark svg{width:100%;height:52px;display:block;margin-top:6px}
.sparl{font-size:10.5px;color:#a5a5a5;margin-top:6px}
.split{height:9px;border-radius:5px;background:#dbe6d9;overflow:hidden;margin:4px 0 6px}
.sfill{height:9px;background:#c9d8ee}
.slab{display:flex;justify-content:space-between;font-size:11px;color:#666}
.trow{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.tn{font-size:12.5px;width:150px;flex:none}.tn.dim,.tp.dim{color:#a0a0a0}
.tb{flex:1;height:6px;background:#f2f2f2;border-radius:3px}
.tf{height:6px;background:var(--accent);border-radius:3px;opacity:.75}.tf.down{background:#c4c4c4}
.tp{font-size:11.5px;font-weight:600;color:var(--accent);width:40px;text-align:right}
.tsep{border-top:1px solid #f0f0f0;margin:7px 0}
/* coverage */
.cov{border:1px solid #d3d3d3;border-radius:10px;padding:14px 16px}
.cov-head{font-size:12px;color:#6a6a6a;margin-bottom:12px}
.cov-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.cov-cell{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.cov-mkt{font-size:10.5px;color:#8a8a8a;text-transform:uppercase;letter-spacing:.05em}
.cov-num{font-size:21px;font-weight:650;margin-top:3px}.cov-num span{font-size:12px;font-weight:500;color:#8a8a8a}
.cov-sub{font-size:10.5px;color:#a5a5a5;margin-top:1px}
.cmini{border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.cm-l{font-size:10px;color:#8a8a8a;text-transform:uppercase;letter-spacing:.05em}
.cm-v{font-size:19px;font-weight:650;margin-top:2px}.cm-v span{font-size:11px;color:#8a8a8a;font-weight:500}
.cov-foot{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;padding-top:10px;border-top:1px solid #f0f0f0;font-size:12px;color:#555}
.cov-note{font-size:11px;color:#a5a5a5;margin-top:9px;line-height:1.5}.cov-note a{color:#777}
.cov-sample{background:#fff8e8;border:1px solid #eadfb8;color:#7a5f14;font-size:12px;
 border-radius:8px;padding:9px 12px;margin-bottom:10px}
.cov-sample code{background:#f3ead0;padding:1px 4px;border-radius:3px}
/* feed */
.grp{margin-bottom:14px}
.grp-h{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#9a9a9a;margin-bottom:7px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
button.f{border:1px solid #dcdcdc;background:#fff;color:#3a3a3a;padding:5px 11px;border-radius:999px;
 font-size:12.5px;cursor:pointer}
button.f:hover{background:#f5f5f5}
button.f.on{background:var(--ink);color:#fff;border-color:var(--ink)}
button.f .n{opacity:.55;margin-left:4px;font-variant-numeric:tabular-nums}
.fbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:14px 0 12px;
 padding-bottom:12px;border-bottom:1px solid var(--line)}
.spacer{flex:1}.count{color:#9a9a9a;font-size:12px}
.card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.card.read{opacity:.45}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:7px}
.tag{font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;padding:2px 7px;border-radius:4px;background:#f0f0f0;color:#555}
.tag.daily{background:#fdeeee;color:#9c2c2c}.tag.weekly{background:#eaf2fd;color:#1f4f8f}.tag.monthly{background:#edf6ee;color:#2b6432}
.src{font-size:12px;color:var(--mute)}
h3{font-size:15px;margin:0 0 6px;font-weight:600;line-height:1.35}
h3 a{color:var(--ink);text-decoration:none}h3 a:hover{text-decoration:underline}
.summ{font-size:13px;color:#444;margin-bottom:9px}
.lens{border-left:3px solid #cfcfcf;background:#fafafa;padding:8px 11px;border-radius:0 6px 6px 0;font-size:12.5px;color:#3d3d3d}
.lens b{color:var(--ink)}
.acts{margin-top:9px}
.acts button{background:none;border:none;padding:0;font-size:12px;color:var(--mute);cursor:pointer}
.acts button:hover{color:var(--ink);text-decoration:underline}
/* sources */
.hubs{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:8px}
.hub{border:1px solid var(--line);border-radius:8px;padding:10px 12px;text-decoration:none;display:block}
.hub:hover{border-color:#bfbfbf;background:#fafafa}
.hub .n{font-size:13px;font-weight:600;color:var(--ink)}.hub .d{font-size:11.5px;color:var(--mute);margin-top:2px}
.foot{font-size:11.5px;color:#a5a5a5;margin-top:22px;border-top:1px solid var(--line);padding-top:12px}
.take{border:1px solid #d8d8d8;border-left:3px solid var(--accent);border-radius:8px;
 padding:12px 15px;margin-bottom:18px;background:#fbfaf9}
.take-l{font-size:9.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:4px}
.take-t{font-size:14px;line-height:1.5;color:#2c2c2c}
.hero{border:1px solid #e6d9d9;border-left:4px solid var(--accent);background:#fcf8f8;border-radius:10px;padding:13px 16px;margin-bottom:20px}
.hero-h{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:8px}
.hero-n{color:#9a8a8a;font-weight:600;margin-left:6px}
.hero-line{font-size:13.5px;color:#2c2c2c;line-height:1.5;padding:2px 0}
.hero-line a{color:var(--ink);font-weight:600}
.hero-tag{font-size:9.5px;font-weight:650;text-transform:uppercase;letter-spacing:.03em;color:var(--accent);background:#f3e3e3;padding:1px 6px;border-radius:4px;margin-left:4px}
.pulse{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
.pulse-c{border:1px solid var(--line);border-radius:8px;padding:9px 10px;cursor:pointer}
.pulse-c:hover{border-color:#bcbcbc;background:#fafafa}
.pl{font-size:10px;color:#6f6f6f;text-transform:uppercase;letter-spacing:.02em;line-height:1.2;min-height:2.1em}
.pv{font-size:18px;font-weight:650;margin-top:2px}
.pd{font-size:10px;font-weight:500;margin-left:1px}
.pd.up{color:#9c2c2c}.pd.down{color:#8a8a8a}.pd.flat{color:#b5b5b5}
@media(max-width:640px){.pulse{grid-template-columns:repeat(3,1fr)}}
.catgrp{margin-bottom:16px}
.catgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.cat{border:1px solid var(--line);border-radius:10px;padding:13px 14px;cursor:pointer}
.cat:hover{border-color:#bcbcbc;background:#fafafa}
.cat-t{font-size:14px;font-weight:600;display:flex;align-items:baseline;gap:6px}
.cat-n{font-size:11px;color:#9a9a9a;font-weight:500;margin-left:auto}
.cat-d{font-size:12px;color:#5f5f5f;margin-top:5px;line-height:1.45}
.catback{font-size:12px;color:#777;cursor:pointer;margin-bottom:12px;display:inline-block}
.catback:hover{color:var(--ink)}
.cat-head{font-size:18px;font-weight:650;margin:0 0 5px;letter-spacing:-.01em}
.cat-lead{font-size:12.5px;color:#666;line-height:1.5;max-width:660px}
@media(max-width:640px){.catgrid{grid-template-columns:1fr}}
@media(max-width:640px){.tiles,.cov-grid{grid-template-columns:repeat(2,1fr)}.panels{grid-template-columns:1fr}
 .dig{grid-template-columns:1fr;gap:3px}.dsrc{white-space:normal}}
"""

JS = """
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let tier='all', layer='all', hideRead=false;
const KEY='aiheor_read_v1';
const read=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
const save=()=>localStorage.setItem(KEY,JSON.stringify([...read]));
const LABEL={daily:'Daily',weekly:'Weekly',monthly:'Monthly'};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const safeUrl=u=>(/^https?:\/\//i.test(u||'')?u:'#');

// tab switching
function goto(name){
  $$('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab===name));
  $$('.view').forEach(v=>v.classList.toggle('on',v.id==='view-'+name));
  if(name==='feed'){ showDir(); }   // always land on the category directory
  window.scrollTo(0,0);
}
function showDir(){ $('#feed-dir').style.display='block'; $('#feed-list').style.display='none'; }
function showList(){ $('#feed-dir').style.display='none'; $('#feed-list').style.display='block'; window.scrollTo(0,0); }
$$('.tab').forEach(t=>t.onclick=()=>goto(t.dataset.tab));
document.addEventListener('click',e=>{
  const g=e.target.closest('[data-goto]'); if(g){e.preventDefault();goto(g.dataset.goto);}
});

// feed
function render(){
  const list=ITEMS.filter(i=>tier==='all'||i.tier===tier)
                  .filter(i=>layer==='all'||i.layer===layer)
                  .filter(i=>!(hideRead&&read.has(i.id)));
  $('#feed').innerHTML = list.map(i=>`
    <div class="card ${read.has(i.id)?'read':''}">
      <div class="meta"><span class="tag ${i.tier}">${LABEL[i.tier]}</span>
        <span class="src">${esc(i.source)} · ${i.date}</span></div>
      <h3><a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener">${esc(i.title)}</a></h3>
      ${i.summary?`<div class="summ">${esc(i.summary)}</div>`:''}
      ${i.lens?`<div class="lens"><b>HEOR lens →</b> ${esc(i.lens)}</div>`:''}
      <div class="acts"><button data-i="${i.id}">${read.has(i.id)?'Mark unread':'Mark read'}</button></div>
    </div>`).join('') || '<div class="dnote">Nothing matches — try another filter.</div>';
  $$('.acts button').forEach(b=>b.onclick=()=>{const id=b.dataset.i;read.has(id)?read.delete(id):read.add(id);save();render();});
  $('#count').textContent=`${list.length} item${list.length===1?'':'s'} · ${read.size} read`;
}
$$('[data-tier]').forEach(b=>b.onclick=()=>{tier=b.dataset.tier;
  $$('[data-tier]').forEach(x=>x.classList.toggle('on',x===b));render();});
$$('.cat').forEach(c=>c.onclick=()=>{
  layer=c.dataset.layer;
  $('#cat-head').textContent=c.dataset.label;
  $('#cat-lead').textContent=c.dataset.desc;
  showList(); render();
});
const showall=$('[data-showall]');
if(showall) showall.onclick=()=>{layer='all';
  $('#cat-head').textContent='All items';
  $('#cat-lead').textContent='Every source across all six categories, unfiltered.';
  showList(); render();};
const back=$('[data-back]');
if(back) back.onclick=()=>showDir();
$('#hide').onclick=e=>{hideRead=!hideRead;e.target.classList.toggle('on',hideRead);render();};
render();
"""

LAYER_LABEL = {"research": "AI research & models", "clinical": "Clinical evidence & trials",
               "heor": "Health economics & HTA", "regulation": "Regulatory & authorisation",
               "access": "Reimbursement & coverage", "industry": "Industry & funding"}

# how the six layers cluster on the Feed tab
LAYER_GROUPS = [
    ("Research & evidence", ["research", "clinical", "heor"]),
    ("Market access & industry", ["regulation", "access", "industry"]),
]

# self-explanatory name + what the feed represents (shown at the top of each list)
LAYER_NAV = {
    "research": ("AI research & models",
        "Frontier AI research, models and methods — arXiv, the major labs, and the main "
        "AI newsletters. The upstream signal: what becomes technically possible before it "
        "reaches medicine."),
    "clinical": ("Clinical evidence & trials",
        "Does it work in patients? Peer-reviewed journals (NEJM AI, Lancet Digital Health, "
        "Nature Medicine, JAMIA), preprints (medRxiv), registered trials (ClinicalTrials.gov), "
        "and Eric Topol's Ground Truths."),
    "heor": ("Health economics & HTA",
        "Is it worth it? Cost-effectiveness, value assessment and HTA methods — Value in "
        "Health, PharmacoEconomics, OHDSI, ISPOR, and PubMed queries on AI in health "
        "technology assessment."),
    "regulation": ("Regulatory & authorisation",
        "Can it be marketed? FDA and EMA guidance, plus AI-enabled device authorisations "
        "(510(k) and PMA) via openFDA."),
    "access": ("Reimbursement & coverage",
        "Will it be paid for? CMS coverage and payment rules, coding (NTAP, CPT), Germany's "
        "DiGA, and NICE HTA decisions — the mechanisms that turn an authorisation into revenue."),
    "industry": ("Industry & funding",
        "The business of health AI — STAT, Endpoints, Fierce, MedTech Dive. Funding rounds, "
        "deals, partnerships and launches."),
}


def render(items, hubs, dead, built, overview="", cov_html="", trend_html=""):
    order = {t: n for n, t in enumerate(TIERS)}
    items.sort(key=lambda i: i["date"], reverse=True)
    items.sort(key=lambda i: order.get(i["tier"], 9))

    counts = {k: sum(1 for i in items if i["layer"] == k) for k in LAYERS}

    tier_btns = "".join(
        f'<button class="f{" on" if t == "all" else ""}" data-tier="{t}">{l}</button>'
        for t, l in [("all", "All"), ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")])

    directory_html = ""
    for gname, keys in LAYER_GROUPS:
        cards = ""
        for k in keys:
            title, desc = LAYER_NAV[k]
            cards += (f'<div class="cat" data-layer="{k}" data-label="{html.escape(title)}" '
                      f'data-desc="{html.escape(desc)}">'
                      f'<div class="cat-t">{html.escape(title)}<span class="cat-n">{counts.get(k,0)}</span></div>'
                      f'<div class="cat-d">{html.escape(desc)}</div></div>')
        directory_html += (f'<div class="catgrp"><div class="grp-h">{html.escape(gname)}</div>'
                           f'<div class="catgrid">{cards}</div></div>')

    hub_html = "".join(
        f'<a class="hub" href="{safe_url(h["url"])}" target="_blank" rel="noopener">'
        f'<div class="n">{html.escape(h["name"])}</div><div class="d">{html.escape(h["note"])}</div></a>'
        for h in hubs)
    warn = f'<div class="foot">Feeds that failed this run: {html.escape(", ".join(dead))}</div>' if dead else ""

    items_json = (json.dumps(items).replace("<", "\\u003c").replace(">", "\\u003e")
                  .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI in Health — Clinical and Market Access Evidence Monitor</title>
<meta name="description" content="Daily monitor of AI across evidence generation, device authorisation and reimbursement — with a clearance-to-coverage tracker across the US, Germany, France and the UK.">
<meta property="og:type" content="website">
<meta property="og:title" content="AI in Health — Clinical and Market Access Evidence Monitor">
<meta property="og:description" content="Daily monitor of AI across evidence generation, device authorisation and reimbursement — with a clearance-to-coverage tracker across the US, Germany, France and the UK.">
<meta property="og:url" content="https://asarmah123.github.io/ai-heor-feed/">
<meta property="og:image" content="https://asarmah123.github.io/ai-heor-feed/preview.png">
<meta name="twitter:card" content="summary_large_image">
<style>{CSS}</style>
</head><body><div class="wrap">
<h1>AI in Health</h1>
<div class="tagline">Clinical and Market Access Evidence Monitor</div>
<div class="sub">Rebuilt every morning · {len(items)} items · updated {built}</div>

<div class="tabs">
  <div class="tab on" data-tab="overview">Overview</div>
  <div class="tab" data-tab="feed">Feed <span class="tabcount">({len(items)})</span></div>
  <div class="tab" data-tab="coverage">Coverage</div>
  <div class="tab" data-tab="trends">Trends</div>
  <div class="tab" data-tab="sources">Sources</div>
</div>

<div id="view-overview" class="view on">{overview or '<div class="dnote">Overview populates on the next build.</div>'}</div>

<div id="view-feed" class="view">
  <div id="feed-dir">
    <div class="dnote" style="margin-bottom:16px">Choose a category to open its feed. Counts are items in the latest build.</div>
    {directory_html}
    <div style="margin-top:6px"><span class="seeall" data-showall="1">Or view everything in one list →</span></div>
  </div>
  <div id="feed-list" style="display:none">
    <div class="catback" data-back="1">← All categories</div>
    <div class="cat-head" id="cat-head"></div>
    <div class="cat-lead" id="cat-lead"></div>
    <div class="fbar">{tier_btns}<span class="spacer"></span>
      <button class="f" id="hide">Hide read</button><span class="count" id="count"></span></div>
    <div id="feed"></div>
  </div>
</div>

<div id="view-coverage" class="view">{cov_html}</div>

<div id="view-trends" class="view">{trend_html}</div>

<div id="view-sources" class="view">
  <div class="sec">Communities &amp; standards bodies</div>
  <div class="hubs">{hub_html}</div>
  <div class="foot">Sources are fetched daily from primary APIs and feeds. Read state is stored in your browser only.</div>
  {warn}
</div>

</div>
<script>const ITEMS={items_json};{JS}</script>
</body></html>""", encoding="utf-8")



def diagnostics(items, cfg, dead):
    """Self-check printed to the build log every run. Verifies each source contributes,
    that item source/layer match the config, and that dates are sane. Catches a feed
    silently breaking or being mis-attributed."""
    from collections import Counter
    print("\n===== FEED DIAGNOSTICS =====")

    # expected source -> layer, from config + the two hardcoded fetchers
    expected = {}
    for grp in ("rss", "gnews", "federal_register", "pubmed", "ctgov", "scrape"):
        for e in cfg.get(grp, []):
            expected[e["name"]] = e["layer"]
    expected["arXiv"] = "research"
    expected["FDA — AI device authorisations"] = "regulation"

    by_src = Counter(i["source"] for i in items)
    by_layer = Counter(i["layer"] for i in items)

    # sporadic-by-design sources (regulators post irregularly); a zero here is normal
    sporadic = {e["name"] for e in cfg.get("federal_register", [])} | {"FDA — AI device authorisations"}

    # 1. per-source counts + zero-yield flags
    print(f"sources contributing: {len(by_src)} / {len(expected)} expected")
    failed = {d.split(":")[0].strip() for d in dead}
    steady_zero = [n for n in expected if by_src.get(n, 0) == 0 and n not in failed and n not in sporadic]
    quiet = [n for n in sporadic if by_src.get(n, 0) == 0 and n not in failed]
    if steady_zero:
        print(f"  ! STEADY sources with zero items (possible breakage): {steady_zero}")
    else:
        print("  ✓ every steady, non-failed source produced at least one item")
    if quiet:
        print(f"  · quiet (normal for regulators): {quiet}")

    # 2. mis-attribution: item source/layer disagreeing with config
    mism = []
    for i in items:
        exp = expected.get(i["source"])
        if exp and exp != i["layer"]:
            mism.append(f"{i['source']}→{i['layer']} (expected {exp})")
    if mism:
        print(f"  ! LAYER MISMATCH: {Counter(mism)}")
    else:
        print("  ✓ every item's layer matches its source's configured bucket")

    # 3. unknown sources (item source not in config) — should only be transformed names
    unknown = {s for s in by_src if s not in expected}
    if unknown:
        print(f"  ! sources not in config (verify): {unknown}")

    # 4. date sanity
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc).date()
    horizon = today - timedelta(days=cfg["settings"]["lookback_days"] + 5)
    future = [i["id"] for i in items if _pdate(i["date"]) and _pdate(i["date"]) > today]
    stale = [i["id"] for i in items if _pdate(i["date"]) and _pdate(i["date"]) < horizon]
    print(f"  dates: {len(future)} future-dated, {len(stale)} older than lookback+5d "
          f"({'ok' if not future else 'CHECK future dates'})")

    # 5. layer distribution of actual items
    print("  layer counts:", dict(by_layer))
    print("============================\n")


def _pdate(s):
    from datetime import datetime
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip the HEOR-lens pass")
    args = ap.parse_args()

    token = os.environ.get("COVERAGE_TOKEN")

    cfg_text, _ = private_get("feeds.yaml", token)      # curated source list = your work
    if cfg_text:
        print("config: private store")
    else:
        cfg_text = (ROOT / "feeds.yaml").read_text()
        print("config: local feeds.yaml")
    cfg = yaml.safe_load(cfg_text)
    st = cfg["settings"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=st["lookback_days"])

    print("fetching RSS…")
    items, dead = fetch_rss(cfg["rss"], cutoff, st["max_per_feed"])
    print(f"  {len(items)} items")

    print("fetching arXiv…")
    ax, d2 = fetch_arxiv(cfg["arxiv"], cutoff, st["max_arxiv"])
    items += ax; dead += d2
    print(f"  {len(ax)} papers")

    print("fetching via Google News…")
    gn, d5 = fetch_gnews(cfg.get("gnews", []), cutoff, st["max_per_feed"])
    items += gn; dead += d5
    print(f"  {len(gn)} items")

    print("fetching Federal Register…")
    fr, d6 = fetch_federal_register(cfg.get("federal_register", []), st["lookback_days"])
    items += fr; dead += d6
    print(f"  {len(fr)} documents")

    print("fetching openFDA device authorisations…")
    of, d7 = fetch_openfda(cfg.get("openfda"), st["lookback_days"])
    items += of; dead += d7
    print(f"  {len(of)} authorisations")

    print("fetching ClinicalTrials.gov…")
    ct, d8 = fetch_ctgov(cfg.get("ctgov", []), st["lookback_days"])
    items += ct; dead += d8
    print(f"  {len(ct)} trials")

    print("fetching PubMed…")
    pm, d4 = fetch_pubmed(cfg.get("pubmed", []), st["lookback_days"])
    items += pm; dead += d4
    print(f"  {len(pm)} papers")

    print("scraping non-RSS pages…")
    sc, d3 = fetch_scrape(cfg["scrape"])
    items += sc; dead += d3
    print(f"  {len(sc)} links")

    # de-dupe, then re-attach any lens text we already paid for
    uniq = {i["id"]: i for i in items}
    items = list(uniq.values())
    cache, cache_sha = load_cache(token)
    for i in items:
        if i["id"] in cache and cache[i["id"]].get("lens"):
            i["lens"] = cache[i["id"]]["lens"]

    if not args.no_llm:
        items = add_lens(items, token)

    now = datetime.now(timezone.utc)
    for i in items:
        cache[i["id"]] = {"lens": i.get("lens", ""), "seen": now.isoformat()}
    save_cache(cache, token, cache_sha)

    cov_data = load_coverage()
    draft = bool(cov_data and cov_data.get("draft"))
    agg = None if draft else coverage_aggregates(cov_data)
    sample = bool(cov_data and cov_data.get("sample"))
    if draft:
        print("  coverage: draft — panel hidden until verified")
    elif agg:
        print(f"  coverage: {agg['n_devices']} devices tracked{' (SAMPLE)' if sample else ''}")

    diagnostics(items, cfg, dead)

    o = overview_stats(items)
    take = weekly_take(items, o, token) if not args.no_llm else ""

    row, history = log_history(items, cfg.get("trend_terms", []), token)
    print(f"  history: {row['total']} items logged for {row['date']} ({len(history)} builds on record)")

    render(items, cfg["hubs"], dead, now.strftime("%d %b %Y %H:%M UTC"),
           overview_html(items, agg, o, history, take), coverage_html(agg, sample), trends_html(items, history))
    print(f"\n✓ docs/index.html — {len(items)} items")
    if dead:
        print(f"! {len(dead)} feed(s) failed: {'; '.join(dead)}")


if __name__ == "__main__":
    main()
