#!/usr/bin/env python3
"""
Build a static AI x HEOR x Market Access feed page.

Fetches RSS feeds, the arXiv API, and a few non-RSS pages; classifies, ranks and
dates every item by transparent rule (no language model); renders docs/index.html.

Run:  python build.py            (full build)
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
# We identify ourselves honestly on every request. A browser UA is used ONLY as a
# last-resort fallback when a source refuses the identifying UA — never to disguise
# who we are. No third-party CORS proxies: we only read endpoints meant to be public.
BOT_UA = {
    "User-Agent": "AI-in-Health-Monitor/1.0 (+https://github.com/asarmah123/ai-health-evidence-monitor)",
    "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
BROWSER_UA = dict(BOT_UA, **{
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
})
UA = BOT_UA   # default headers for the JSON-API fetchers (PubMed, Federal Register, openFDA, ctgov)
LAYERS = ["research", "clinical", "regulation", "heor", "access", "industry"]
STAGE_COLOR = {"research": "#6a4c93", "clinical": "#9c2c44", "regulation": "#2f6f9f",
               "heor": "#1f8a70", "access": "#b0842b", "industry": "#64748b"}
TIERS = ["daily", "weekly", "monthly"]

# Respectful pacing: never fire requests faster than one every _MIN_GAP seconds.
_MIN_GAP = 0.5
_last_req = [0.0]


def _throttle():
    dt = time.time() - _last_req[0]
    if dt < _MIN_GAP:
        time.sleep(_MIN_GAP - dt)
    _last_req[0] = time.time()


def get(url, timeout=25):
    """Fetch a URL politely: identifying UA, throttle, one transient-error retry,
    and a single browser-UA fallback if the source refuses the bot UA (401/403/406)."""
    for attempt in range(2):
        _throttle()
        try:
            r = requests.get(url, headers=BOT_UA, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (401, 403, 406):
                break                       # UA refused → try a browser UA once
            if attempt == 0 and code in (429, 500, 502, 503, 504):
                time.sleep(2); continue     # transient server error → back off once
            raise
        except requests.RequestException:
            if attempt == 0:
                time.sleep(2); continue     # timeout / connection blip → retry once
            raise
    _throttle()
    r = requests.get(url, headers=BROWSER_UA, timeout=timeout)
    r.raise_for_status()
    return r


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


def when_from(entry):
    """Aware datetime for a feed entry, or None if it carries no usable date.
    We never invent a date: undated items are marked, not stamped with 'today'.
    This keeps the site's promise that dates are read from sources, never estimated."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st:
        try:
            return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
        except (OverflowError, ValueError, TypeError):
            pass
    # fallback: some feeds carry the date only as an unstructured string field.
    # Parsed with the stdlib (RFC822 then ISO8601) — still read from the source, never invented.
    from email.utils import parsedate_to_datetime
    for key in ("published", "updated", "dc_date", "date", "pubdate", "issued"):
        raw = entry.get(key)
        if not raw:
            continue
        for parse in (parsedate_to_datetime,
                      lambda r: datetime.fromisoformat(str(r).replace("Z", "+00:00"))):
            try:
                dt = parse(raw)
                if dt:
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


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
            dead.append(f"{s['name']}: HTTP {getattr(e.response, 'status_code', '?')} [{s['url']}]")
            continue
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}: {str(e)[:120]} [{s['url']}]")
            continue
        if not parsed.entries:
            # empty feed is not an error — a low-cadence source is often simply quiet.
            # The tier-based health check flags this only for daily sources.
            print(f"  · {s['name']}: feed returned no entries", file=sys.stderr)
            continue

        kept = 0
        for e in parsed.entries:
            if kept >= cap:
                break
            when = when_from(e)
            if when is not None and when < cutoff:
                continue
            link = e.get("link")
            title = clean(e.get("title", ""), 200)
            if not link or not title:
                continue
            items.append({
                "id": uid(link), "title": title, "url": link,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": when.strftime("%Y-%m-%d") if when else "",
                "summary": clean(e.get("summary", "")),
            })
            kept += 1
    return items, dead


def fetch_arxiv(cfg, cutoff, cap):
    cats = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    url = ("https://export.arxiv.org/api/query?"
           f"search_query={requests.utils.quote(cats)}"
           "&sortBy=submittedDate&sortOrder=descending&max_results=120")
    # arXiv can be slow or blocked from cloud IPs, but we must not stall the daily build.
    # Bounded to 3 quick attempts (~1 min worst case): one retry on the bot UA for rate-limit
    # recovery, then a browser-UA fallback. Never a proxy — the API is fully public.
    parsed, last = None, "unknown error"
    for ua, wait in ((BOT_UA, 0), (BOT_UA, 3), (BROWSER_UA, 0)):
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(url, headers=ua, timeout=20)
            r.raise_for_status()
            p = feedparser.parse(r.content)
            if p.entries:
                parsed = p
                break
            last = "no entries returned"
        except requests.HTTPError as e:
            last = f"HTTP {getattr(e.response, 'status_code', '?')}"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
    if parsed is None:
        return [], [f"arXiv: {last} [export.arxiv.org]"]

    terms = [t.lower() for t in cfg["boost_terms"]]
    scored = []
    for e in parsed.entries:
        when = when_from(e)
        if when is not None and when < cutoff:
            continue
        title = clean(e.get("title", ""), 200)
        blob = (title + " " + e.get("summary", "")).lower()
        score = sum(1 for t in terms if t in blob)
        scored.append((score, {
            "id": uid(e.link), "title": title, "url": e.link,
            "source": "arXiv", "tier": "daily", "layer": "research",
            "date": when.strftime("%Y-%m-%d") if when else "",
            "summary": clean(e.get("summary", "")),
        }))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:cap]], []


_SC_MON = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,
           "oct":10,"nov":11,"dec":12,"january":1,"february":2,"march":3,"april":4,"june":6,
           "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
_SC_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b")
_SC_MDY = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")


def _sc_mk(y, mon, d):
    if not mon or not (1 <= d <= 31):
        return ""
    try:
        datetime(y, mon, d)          # validate (rejects e.g. 31 Feb)
        return f"{y:04d}-{mon:02d}-{d:02d}"
    except ValueError:
        return ""


def _scrape_date(a):
    """Read a visible 'DD Month YYYY' / 'Month DD, YYYY' date from the link's own small
    container, if present. Returns 'YYYY-MM-DD' or '' — reads a real date, never invents."""
    node = a
    for _ in range(5):
        node = getattr(node, "parent", None)
        if node is None:
            break
        txt = node.get_text(" ", strip=True)
        if len(txt) > 700:           # too large → multi-article wrapper; stop (avoid wrong date)
            break
        m = _SC_DMY.search(txt)
        if m:
            return _sc_mk(int(m.group(3)), _SC_MON.get(m.group(2).lower()), int(m.group(1)))
        m = _SC_MDY.search(txt)
        if m:
            return _sc_mk(int(m.group(3)), _SC_MON.get(m.group(1).lower()), int(m.group(2)))
    return ""


def fetch_scrape(sources):
    items, dead = [], []
    for s in sources:
        try:
            soup = BeautifulSoup(get(s["url"]).text, "html.parser")
        except requests.HTTPError as e:
            dead.append(f"{s['name']}: HTTP {getattr(e.response, 'status_code', '?')} [{s['url']}]")
            continue
        except Exception as e:
            dead.append(f"{s['name']}: {type(e).__name__}: {str(e)[:120]} [{s['url']}]")
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
                "date": _scrape_date(a),   # read a visible date if present; '' otherwise (never invented)
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
            dead.append(f"{s['name']}: {type(e).__name__}: {str(e)[:120]} [PubMed E-utilities]")
            continue

        for pmid in pmids:
            rec = res.get(pmid)
            if not rec:
                continue
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            raw = (rec.get("sortpubdate") or rec.get("pubdate") or "")[:10].replace("/", "-")
            date = raw if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) else ""
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
            dead.append(f"{s['name']} (via Google News): {type(e).__name__}: {str(e)[:100]}")
            continue

        kept = 0
        for e in parsed.entries:
            if kept >= cap:
                break
            when = when_from(e)
            if when is not None and when < cutoff:
                continue
            title = clean(e.get("title", ""), 200)
            title = re.sub(r"\s+-\s+[^-]+$", "", title)   # strip the trailing " - Publisher"
            items.append({
                "id": uid(e.link), "title": title, "url": e.link,
                "source": s["name"], "tier": s["tier"], "layer": s["layer"],
                "date": when.strftime("%Y-%m-%d") if when else "", "summary": "",
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
            dead.append(f"{s['name']}: {type(e).__name__}: {str(e)[:120]} [Federal Register API]")
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
            dead.append(f"openFDA {label}: HTTP {getattr(e.response, 'status_code', '?')} [api.fda.gov/device/{ep}]")
            continue
        except Exception as e:
            dead.append(f"openFDA {label}: {type(e).__name__}: {str(e)[:120]} [api.fda.gov/device/{ep}]")
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
                date = ""
            link = f"{base_link}{num}"
            items.append({
                "id": uid(link), "title": f"FDA {label} {verb}: {clean(name, 150)}",
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
            dead.append(f"{s['name']}: {type(e).__name__}: {str(e)[:120]} [ClinicalTrials.gov API v2]")
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
                "date": when[:10] if when else "",
                "summary": clean(f"{bits} — primary endpoint: {primary}" if primary else bits, 220),
            })
    return items, dead


def log_history(items, terms, token=None, health=None):
    """One row per build: per-layer counts, counts for each tracked term, and a compact
    per-source health snapshot. Persisted to the private data repo via the GitHub
    Contents API (SHA-based update) — no git push from the Action, so no merge/HEAD state."""
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
    _bc = _body_role_counts(items)
    row = {
        "date": today,
        "total": len(items),
        "layers": {l: sum(1 for i in items if i["layer"] == l) for l in LAYERS},
        "terms": {t: blob.count(t.lower()) for t in terms},
        # per-body counts (regulators + payers) so "above/below its recent norm" becomes
        # computable as history accrues — the honest, baseline-backed "what's unusual"
        "bodies": {name: cnt for role in ("regulator", "payer")
                   for name, cnt in _bc.get(role, [])},
    }
    if health:
        # compact health footprint, so "dead 1 day vs dead 5 days" becomes visible over time
        row["health"] = {
            "contributing": health.get("contributing"),
            "expected": health.get("expected"),
            "silent": health.get("zero_steady", []),
            "failed": health.get("failed", []),
            "undated": health.get("undated", 0),
        }
    hist = [h for h in hist if h.get("date") != today] + [row]   # one row per day, last write wins
    hist = hist[-400:]                                            # ~13 months
    text_out = json.dumps(hist, indent=1)
    if not private_put("history.json", text_out, token, sha, f"history {today}"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_out)
    return row, hist


# No language model is used anywhere in this build. Classification, ranking, dating
# and every count are rule-based and reproducible. (Earlier revisions carried an
# LLM "editor's take" and per-item "HEOR lens"; both were removed to keep the
# deterministic, no-model guarantee true end to end.)


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
    "Switzerland": "Europe", "Italy": "Europe", "Sweden": "Europe", "Netherlands": "Europe",
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
    "CMS": "payer", "NICE": "payer", "G-BA": "payer", "IQWiG": "payer", "HAS": "payer", "BfArM": "payer",
    "PBAC": "payer", "MSAC": "payer", "HIRA": "payer", "NECA": "payer", "Chuikyo": "payer",
    "HITAP": "payer", "ACE": "payer", "CADTH": "payer", "MOHAP": "regulator",
    "ICER": "payer", "AIFA": "payer", "TLV": "payer", "Zorginstituut": "payer",
    "Swissmedic": "regulator", "Health Canada": "regulator", "NHSA": "payer",
    # professional societies & HTA networks (no binding decisions)
    "ISPOR": "professional", "HTAi": "professional", "INAHTA": "professional",
}

# bodies distinctive enough to match safely in free text (no source feed of their own).
# Ambiguous acronyms (HAS, NICE, CMS, FDA, ACE) are matched by SOURCE only, never text.
SAFE_TEXT_BODIES = {"PMDA", "NMPA", "TGA", "MFDS", "HSA", "CDSCO", "SFDA", "SAHPRA",
                    "PBAC", "MSAC", "HIRA", "NECA", "Chuikyo", "MHRA", "BfArM", "IQWiG",
                    "G-BA", "ISPOR", "HTAi", "HITAP", "CADTH", "MOHAP",
                    "ICER", "AIFA", "TLV", "Zorginstituut", "Swissmedic", "Health Canada", "INAHTA", "NHSA"}

# source-type: a cross-cutting lens for the Evidence filter — what KIND of source an
# item is, independent of its lifecycle stage. Rule-based on source name / URL only.
_ST_REGULATOR = ("FDA", "EMA", "MHRA", "Federal Register", "PMDA", "NMPA", "TGA", "MFDS",
                 "SFDA", "Swissmedic", "Health Canada", "MOHAP", "HSA", "CDSCO", "SAHPRA")
_ST_PAYER = ("CMS", "NICE", "G-BA", "IQWiG", "HAS", "BfArM", "CADTH", "PBAC", "MSAC",
             "HIRA", "NECA", "AIFA", "TLV", "Zorginstituut", "HITAP", "ACE", "ICER", "NHSA")
_ST_JOURNAL = ("PubMed", "NEJM", "Lancet", "Nature", "JAMIA", "JAMA", "BMJ",
               "Value in Health", "PharmacoEconomics", "Ground Truths")
_ST_INDUSTRY = ("STAT", "Endpoints", "Fierce", "MedTech", "MassDevice", "MobiHealth")


def source_type(i):
    s = i.get("source", "")
    u = i.get("url", "").lower()
    if "clinicaltrials" in u:
        return "Trial registry"
    if "arxiv" in u or "arxiv" in s.lower() or "medrxiv" in u or "medrxiv" in s.lower():
        return "Preprint / research"
    if any(k in s for k in _ST_REGULATOR):
        return "Regulator"
    if any(k in s for k in _ST_PAYER):
        return "HTA / payer"
    if any(k in s for k in _ST_JOURNAL):
        return "Journal / evidence"
    if any(k in s for k in _ST_INDUSTRY):
        return "Industry press"
    return "Other"


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
        ("China", ["nmpa", "nhsa", "china"]),
        ("Australia", ["tga", "pbac", "msac", "australia"]),
        ("South Korea", ["mfds", "hira", "neca", "south korea", "korea"]),
        ("India", ["cdsco", "india"]),
        ("Singapore", ["hsa singapore", "singapore", "agency for care effectiveness"]),
        ("Thailand", ["hitap", "thailand", "thai fda"]),
        ("Canada", ["cadth", "health canada", "canada"]),
        ("Switzerland", ["swissmedic", "switzerland"]),
        ("Italy", ["aifa", "italy"]),
        ("Sweden", ["tlv", "sweden", "tandvard"]),
        ("Netherlands", ["zorginstituut", "netherlands"]),
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
            if (b in src) or (b in SAFE_TEXT_BODIES and re.search(rf"\b{re.escape(b.lower())}\b", text)):
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


_ORG_HINTS = ("univ", "hospital", "institut", "inc", "ltd", "corp", "gmbh", "co.", "co ",
              "center", "centre", "foundation", "college", "health", "medical", "clinic",
              "llc", " ag", " ab", " sa", "nhs", "trust", "agency", "assoc", "society",
              "consortium", "network", "laborator", "labs", "pharma", "therapeut", "science",
              "systems", "technolog", "genom", "diagnostic", "imaging", "school", "company",
              "department", "national", "academy", "board", "council", "ministry", "authority",
              "group", "plc", "biosci")


def _is_org(name):
    """Keep organisations, drop individual investigator names (ClinicalTrials.gov lists some
    sponsors as a person). Keyword + shape rules, no ML."""
    low = name.lower()
    if any(h in low for h in _ORG_HINTS):
        return True
    if any(ch.isdigit() for ch in name) or any(ch in name for ch in ".,&/()"):
        return True
    toks = name.split()
    if len(toks) == 1:
        return True
    if 2 <= len(toks) <= 3 and all(t[:1].isupper() and t.replace("-", "").replace("'", "").isalpha() for t in toks):
        return False
    return True


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
        if not _is_org(name):
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
        ("NICE EVA", ["early value assessment", "nice eva"]),
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


# Plain-language, honest reasons for why an item is the day's top story. We state the
# RULE that surfaced it — never an invented explanation of significance.
WHY_TEXT = {
    "Device authorisations": "Regulatory clearance is the first step toward deployment. "
                             "FDA publication may lag the decision date.",
    "Trials · economic endpoint": "An economic endpoint marks a trial designed to support the "
                                  "reimbursement case, not just clinical approval.",
    "Regulatory actions": "A move by a major regulator or HTA body — the decisions that shape whether, "
                          "and how, an AI product reaches patients.",
}

# Short, factual significance line per digest group — explains why the CATEGORY matters
# for market access, not why any specific event happened. Descriptive, never causal.
WHY_MATTERS = {
    "Device authorisations": "Authorisation is the first step from evidence toward commercial deployment.",
    "Trials · economic endpoint": "Economic endpoints build the reimbursement case, not just clinical proof.",
    "Regulatory actions": "Major-regulator and HTA actions shape how — and whether — a product reaches patients.",
}

# Category (not cause) for each tracked term — lets Trends say WHAT KIND of signal a
# term is, without claiming why it moved.
TERM_CLASS = {
    "NTAP": "Payment / reimbursement", "CPT": "Payment / reimbursement",
    "reimbursement": "Payment / reimbursement", "coverage": "Payment / reimbursement",
    "DiGA": "Payment / reimbursement", "digital therapeutic": "Payment / reimbursement",
    "Early Value Assessment": "Payment / reimbursement",
    "health technology assessment": "HEOR / evidence", "cost-effectiveness": "HEOR / evidence",
    "real-world evidence": "HEOR / evidence", "systematic review": "HEOR / evidence",
    "EU AI Act": "Regulatory", "510(k)": "Regulatory", "De Novo": "Regulatory",
    "software as a medical device": "Regulatory", "predetermined change control": "Regulatory",
    "large language model": "AI capability", "agent": "AI capability", "foundation model": "AI capability",
    "validation": "Evaluation / safety", "bias": "Evaluation / safety", "hallucination": "Evaluation / safety",
}


# Display-only aliases to disambiguate terms whose bare word is ambiguous.
# Matching still uses the raw term; only the rendered label changes.
TERM_DISPLAY = {"coverage": "Coverage (reimbursement)", "agent": "AI agent"}

# Transparent, rules-based importance score. Every point is attributable to a named
# reason (shown to the user), so ranking is explainable — never a black box.
MAJOR_BODIES = ("FDA", "CMS", "EMA", "NICE", "MHRA", "G-BA", "HAS", "CADTH", "PMDA")


def rank_score(i):
    """Return (score, [reasons]) for one item. Higher = more consequential for a
    market-access reader. Purely deterministic — no model, no inference."""
    s, reasons = 0, []
    src, layer, url = i.get("source", ""), i.get("layer", ""), i.get("url", "")

    if src == "FDA — AI device authorisations":
        s += 5; reasons.append("New device authorisation")
    if any(b in src for b in MAJOR_BODIES):
        s += 3; reasons.append("Major regulator / HTA body")

    if layer == "access":
        s += 3; reasons.append("Reimbursement / coverage")
    elif layer == "regulation":
        s += 2; reasons.append("Regulatory / authorisation")

    if _econ_endpoint(i):
        s += 4; reasons.append("Trial with an economic endpoint")
    if "clinicaltrials" in url:
        s += 1; reasons.append("Registered trial")
    if layer == "heor":
        s += 2; reasons.append("HEOR / value evidence")

    if i.get("tier") == "daily":
        s += 1; reasons.append("High-cadence source")

    d = _pdate(i.get("date", ""))
    if d:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc).date() - d).days
        if age <= 2:
            s += 2; reasons.append("Published in the last 2 days")
        elif age <= 7:
            s += 1; reasons.append("Published this week")

    return s, reasons


# Plain-language expansions for acronyms shown in the panels, surfaced as hover
# tooltips (<abbr>) so non-specialists aren\u2019t lost. Educational only; no data change.
GLOSSARY = {
    "FDA": "US Food and Drug Administration",
    "EMA": "European Medicines Agency",
    "MHRA": "UK Medicines and Healthcare products Regulatory Agency",
    "NICE": "UK National Institute for Health and Care Excellence",
    "CMS": "US Centers for Medicare & Medicaid Services",
    "CADTH": "Canada\u2019s Drug Agency (formerly CADTH)",
    "IQWiG": "Germany\u2019s Institute for Quality and Efficiency in Health Care",
    "G-BA": "Germany\u2019s Federal Joint Committee (Gemeinsamer Bundesausschuss)",
    "HAS": "France\u2019s Haute Autorit\u00e9 de Sant\u00e9",
    "BfArM": "Germany\u2019s Federal Institute for Drugs and Medical Devices \u2014 regulator that also runs the DiGA reimbursement fast-track",
    "HIRA": "South Korea\u2019s Health Insurance Review & Assessment Service",
    "PBAC": "Australia\u2019s Pharmaceutical Benefits Advisory Committee",
    "MSAC": "Australia\u2019s Medical Services Advisory Committee",
    "PMDA": "Japan\u2019s Pharmaceuticals and Medical Devices Agency",
    "NMPA": "China\u2019s National Medical Products Administration",
    "TGA": "Australia\u2019s Therapeutic Goods Administration",
    "MFDS": "South Korea\u2019s Ministry of Food and Drug Safety",
    "HSA": "Singapore\u2019s Health Sciences Authority",
    "SFDA": "Saudi Food and Drug Authority",
    "SAHPRA": "South African Health Products Regulatory Authority",
    "NHSA": "China\u2019s National Healthcare Security Administration",
    "AIFA": "Italy\u2019s Medicines Agency (Agenzia Italiana del Farmaco) \u2014 drug regulator and pricing / reimbursement authority",
    "TLV": "Sweden\u2019s Dental and Pharmaceutical Benefits Agency",
    "ICER": "US Institute for Clinical and Economic Review",
    "Zorginstituut": "Netherlands\u2019 National Health Care Institute",
    "Swissmedic": "Switzerland\u2019s therapeutic products authority",
    "ACE": "Singapore\u2019s Agency for Care Effectiveness",
    "NECA": "South Korea\u2019s National Evidence-based Healthcare Collaborating Agency",
    "Chuikyo": "Japan\u2019s Central Social Insurance Medical Council",
    "HITAP": "Thailand\u2019s Health Intervention and Technology Assessment Program",
    "MOHAP": "UAE Ministry of Health and Prevention",
    "Health Canada": "Canada\u2019s federal health department",
    "ISPOR": "The Professional Society for Health Economics and Outcomes Research",
    "HTAi": "Health Technology Assessment international",
    "INAHTA": "International Network of Agencies for Health Technology Assessment",
    "DiGA": "Germany\u2019s fast-track reimbursement path for digital health apps",
    "NTAP": "US Medicare New Technology Add-on Payment",
    "CPT": "US Current Procedural Terminology billing codes",
}


def gloss(lbl):
    """Wrap a known acronym in an <abbr> tooltip; otherwise just escape it."""
    v = str(lbl)
    if v in GLOSSARY:
        return f'<abbr title="{html.escape(GLOSSARY[v])}">{html.escape(v)}</abbr>'
    return html.escape(v)


def _fmt_date(d):
    """Human-readable date for the Top Story ('21 Jul 2026'); honest when missing."""
    dt = _pdate(d)
    return f"{dt.day} {dt.strftime('%b %Y')}" if dt else "date unknown"


_KIND = {"research": "Research", "clinical": "Clinical", "regulation": "Regulatory",
         "heor": "Health economics", "access": "Reimbursement", "industry": "Industry"}


def overview_html(items, agg, o, history=None, take=""):
    # ---- pipeline pulse: one cell per category, mirrors the Feed tabs
    prior = (history or [])[:-1]
    SHORT = {"research": "Research", "clinical": "Clinical", "heor": "HEOR",
             "regulation": "Regulatory", "access": "Reimbursement", "industry": "Industry"}
    LONG = {"research": "AI research", "clinical": "Clinical",
            "heor": "Health economics (HEOR)", "regulation": "Regulatory",
            "access": "Reimbursement", "industry": "Industry"}
    JLABEL = {"research": "Research", "clinical": "Clinical evidence",
              "regulation": "Regulatory & authorisation", "heor": "HEOR",
              "access": "Reimbursement & coverage", "industry": "Market activity"}
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
    def _rarity(k):
        seq = [h["layers"][k] for h in prior if isinstance(h.get("layers"), dict) and k in h["layers"]]
        if len(seq) < 5:
            return ""
        cur = o["layers"][k]
        run_hi = run_lo = 0
        for v in reversed(seq):
            if cur > v: run_hi += 1
            else: break
        for v in reversed(seq):
            if cur < v: run_lo += 1
            else: break
        if run_hi >= 5:
            return f'<div class="jnote">highest in {run_hi + 1} builds</div>'
        if run_lo >= 5:
            return f'<div class="jnote">lowest in {run_lo + 1} builds</div>'
        return ""
    journey = "".join(
        (('<div class="jarrow">\u2193</div>' if idx else '')
         + f'<div class="jstep" role="button" tabindex="0" data-goto="feed" data-layer="{k}" data-label="{html.escape(LAYER_NAV[k][0])}" data-desc="{html.escape(LAYER_NAV[k][1])}" style="border-left:4px solid {STAGE_COLOR[k]}"><div class="jlabel">{JLABEL[k]}</div>'
           f'<div class="jval">{o["layers"][k]} {pdelta(k)}{_rarity(k)}</div></div>')
        for idx, k in enumerate(LAYERS))
    journey_html = f'<div class="journey">{journey}</div>'
    # compact one-line lifecycle strip for the Home page (detailed counts live in Analysis)
    JSHORT = {"research": "Research", "clinical": "Clinical", "regulation": "Regulatory",
              "heor": "HEOR", "access": "Coverage", "industry": "Market"}
    _jnodes = []
    for k in LAYERS:
        _on = o["layers"].get(k, 0) > 0
        _st = f'background:{STAGE_COLOR[k]};border-color:{STAGE_COLOR[k]}' if _on else ''
        _jnodes.append(
            f'<button class="jnode" data-goto="feed" data-layer="{k}" '
            f'data-label="{html.escape(LAYER_NAV[k][0])}" data-desc="{html.escape(LAYER_NAV[k][1])}" '
            f'title="{JSHORT[k]}: {o["layers"].get(k, 0)} today">'
            f'<span class="jdot{" on" if _on else ""}" style="{_st}"></span>'
            f'<span class="jnl">{JSHORT[k]}</span></button>')
    journey_strip = ('<div class="jstrip">' + '<span class="jsep">→</span>'.join(_jnodes) + '</div>')

    # ---- the two market-access gates, then leading indicators
    def render_tiles(rows):
        return "".join(
            f'<div class="tile"><div class="tl">{t}</div><div class="tv">{v}</div>'
            f'<div class="ts">{sub if "&" in sub else html.escape(sub)}</div></div>' for t, v, sub in rows)
    gate_tiles = [
        ("Authorisation", len(o["clears"]),
         "Recent FDA AI authorisations — can it be sold? (published with a lag)."),
        ("Coverage", len(o["coverage_actions"]),
         "Recent CMS and NICE payment decisions — will it be paid for?"),
    ]
    if not o["trials"]:
        _trial_sub = "No AI trials in this build."
    elif not o["econ"]:
        _trial_sub = f"None of the {len(o['trials'])} AI trials in this build included an economic endpoint."
    else:
        _trial_sub = f"{len(o['econ'])} of {len(o['trials'])} AI trials in this build included an economic endpoint."
    ind_tiles = [
        ("Economic-endpoint trials", len(o["econ"]), _trial_sub),
        ("HTA &amp; value papers", len(o["papers"]),
         "Peer-reviewed HTA and value studies in this build." if o["papers"]
         else "No HTA or value studies in this build."),
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
            gitems = sorted(gitems, key=lambda i: -rank_score(i)[0])
            grows = "".join(
                f'<a class="dig" href="{safe_url(i["url"])}" target="_blank" rel="noopener">'
                f'<span class="dttl">{html.escape(i["title"])}</span>'
                f'<span class="dsrc">{html.escape(i["source"])} · {i["date"] or "date unknown"}</span></a>'
                for i in gitems)
            wm = WHY_MATTERS.get(why, "")
            wm_html = f'<div class="digwhy"><b>Why it matters:</b> {wm}</div>' if wm else ""
            boxes += (f'<details class="digbox"><summary class="digbox-h">{why}'
                      f'<span class="digbox-n">{len(gitems)}</span></summary>{wm_html}{grows}</details>')
        _WHY_PHRASE = {"Device authorisations": "new device authorisations",
                       "Trials · economic endpoint": "trials with an economic endpoint",
                       "Regulatory actions": "actions from a major regulator (FDA, CMS, EMA, NICE)"}
        _cats = "; ".join(_WHY_PHRASE[w] for w in groups if w in _WHY_PHRASE) or "the day\u2019s highest-consequence updates"
        digest = f'<details class="ovsec" open><summary class="secsum">Priority updates</summary><div class="seccap">The highest-consequence updates today, pulled to the top by rule — {_cats}.</div><div class="digboxes">{boxes}</div></details>'
    else:
        digest = ('<details class="ovsec" open><summary class="secsum">Priority updates</summary>'
                  '<div class="seccap">The highest-consequence updates today, pulled to the top by rule — new device authorisations, trials with an economic endpoint, and actions from a major regulator (FDA, CMS, EMA, NICE).</div>'
                  '<div class="dnote">No device authorisations, economic-endpoint trials, or major '
                  'regulatory actions today. A quiet day.</div></details>')

    # --- shared bar-panel builder ---
    def bar_panel(title, sub, rows, empty, color="#9c2c2c"):
        subhtml = f'<div class="psub">{sub}</div>' if sub else ""
        if rows:
            head = title + (f" (top 6 of {len(rows)})" if len(rows) > 6 else "")
            peak = rows[0][1] or 1
            bars = "".join(
                f'<div class="trow"><div class="tn">{gloss(lbl)}</div>'
                f'<div class="tb"><div class="tf" style="width:{n/peak*100:.0f}%;background:{color}"></div></div>'
                f'<div class="tp" style="color:{color}">{n}</div></div>' for lbl, n in rows[:6])
            return f'<div class="panel"><div class="ph">{head}</div>{subhtml}{bars}</div>'
        return f'<div class="panel"><div class="ph">{title}</div><div class="psub">{empty}</div></div>'

    bodies = o.get("bodies", {})
    GEO_C = "#5f6b7a"
    def geo_rows(rows):
        if not rows:
            return '<div class="psub" style="margin-bottom:2px">none today</div>'
        peak = rows[0][1] or 1
        return "".join(
            f'<div class="trow"><div class="tn">{gloss(lbl)}</div>'
            f'<div class="tb"><div class="tf" style="width:{n/peak*100:.0f}%;background:{GEO_C}"></div></div>'
            f'<div class="tp" style="color:{GEO_C}">{n}</div></div>' for lbl, n in rows[:6])
    _nc = len(o.get("countries", []))
    _clabel = f"By country (top 6 of {_nc})" if _nc > 6 else "By country"
    geo_panel = (f'<div class="panel"><div class="ph">Geography</div>'
                 f'<div class="subh">By region</div>{geo_rows(o.get("macro", []))}'
                 f'<div class="subh" style="margin-top:9px">{_clabel}</div>{geo_rows(o.get("countries", []))}</div>')
    regulators_panel = bar_panel("Regulators", "",
                                 bodies.get("regulator", []), "No regulator activity today.", color="#2f6f9f")
    payers_panel = bar_panel("HTA &amp; payers", "",
                             bodies.get("payer", []), "No HTA / payer activity today.", color="#1f8a70")
    clinfocus = bar_panel("Clinical areas", "",
                          o.get("focus", []), "No specialty clearly identified today.", color="#9c2c44")
    pathway = bar_panel("Coverage pathways", "",
                        o.get("pathways", []), "None mentioned today.", color="#b0842b")

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
    ln_mover = ln_body = ln_term = ln_region = ln_clin = None
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
            ln_mover = (f'<b>{LONG[k]}</b> activity {"increased" if d > 0 else "eased"} '
                        f'({"+" if d > 0 else "−"}{abs(d):.0f} vs last week)')
    # is the day's leading body unusual vs its OWN recent norm? (needs body history to accrue)
    allb = o["bodies"]["regulator"] + o["bodies"]["payer"]
    if allb:
        bname, bcnt = max(allb, key=lambda x: x[1])
        bbase = [h["bodies"].get(bname, 0) for h in prior_h[-28:] if h.get("bodies")]
        if len(bbase) >= 3:
            bavg = sum(bbase) / len(bbase)
            if bcnt - bavg >= 2:
                ln_body = (f'<b>{html.escape(bname)}</b> above its recent norm '
                           f'({bcnt} vs ~{bavg:.0f})')
    # what's unusual: the biggest term riser vs its own recent baseline (works now — terms tracked)
    if history and len(prior_h) >= 3:
        tt = history[-1].get("terms", {})
        tmoves = []
        for term, tnow in tt.items():
            tbase = [h.get("terms", {}).get(term, 0) for h in prior_h[-28:]]
            tavg = sum(tbase) / len(tbase) if tbase else 0
            if tnow > 0 and tnow - tavg >= 1.5 and (tavg == 0 or tnow / tavg >= 2):
                tmoves.append((tnow - tavg, term, tnow, tavg))
        if tmoves:
            tmoves.sort(key=lambda x: -x[0])
            _, tterm, tnow, tavg = tmoves[0]
            _base_txt = f'~{tavg:.0f}' if tavg >= 1 else '~1'
            ln_term = f'<b>{html.escape(tterm)}</b> mentions elevated ({tnow} vs {_base_txt} typical build)'
    # single most consequential item → promoted into its own dominant card (topstory)
    hpicks = _digest(o)
    if hpicks:
        # Top story prefers the most consequential RECENT item, so a weeks-old
        # authorisation does not dominate a fresh major-regulator action. If nothing
        # is recent, fall back to the top-ranked item (its lag is disclosed below).
        _today = datetime.now(timezone.utc).date()
        def _fresh(it):
            d = _pdate(it.get("date", ""))
            return d is not None and (_today - d).days <= 10
        why, hi = next(((w, it) for w, it in hpicks if _fresh(it)), hpicks[0])
        why_text = WHY_TEXT.get(why, why)
        _kind = _KIND.get(hi.get("layer", ""), "")
        _kind_html = f'<span class="ts-kind">{_kind}</span> · ' if _kind else ""
        topstory = (f'<div class="topstory" data-open="{html.escape(safe_url(hi["url"]))}"><div class="topstory-l">Featured story</div>'
                    f'<a class="topstory-t" href="{safe_url(hi["url"])}" target="_blank" rel="noopener">{html.escape(hi["title"])}</a>'
                    f'<div class="topstory-m">{_kind_html}<span class="ts-src">{html.escape(hi["source"])}</span> · '
                    f'<span class="ts-date">{_fmt_date(hi["date"])}</span></div>'
                    f'<div class="topstory-why"><b>Why it matters:</b> {html.escape(why_text)}</div></div>')
    else:
        topstory = ('<div class="topstory quiet"><div class="topstory-l">Featured story</div>'
                    '<div class="topstory-t2">A quiet day</div>'
                    '<div class="topstory-why">No new device authorisations, economic-endpoint trials, '
                    'or major-regulator actions in this build.</div></div>')
    # most active market + body
    if o.get("macro"):
        reg = o["macro"][0]
        allb_reg = o["bodies"]["regulator"]
        allb = allb_reg + o["bodies"]["payer"]
        tb = max(allb, key=lambda x: x[1]) if allb else None
        line = f'Highest activity by region: <b>{html.escape(reg[0])}</b> ({reg[1]})'
        if tb:
            _role = "regulator" if tb in allb_reg else "HTA / payer body"
            line += f' · Most active {_role}: <b>{html.escape(tb[0])}</b> ({tb[1]})'
        ln_region = line
    # dominant clinical area — a real distribution insight (no fabricated cause)
    focus = o.get("focus", [])
    if focus:
        ln_clin = f"<b>{html.escape(focus[0][0])}</b> led clinical evidence in this build"
    hero_lines = [x for x in (ln_mover, ln_term, ln_clin, ln_body) if x][:3]  # region/regulator lives in Analysis
    if hero_lines:
        hl = "".join(f'<div class="hero-line">{x}</div>' for x in hero_lines)
        hero = (f'<div class="hero"><div class="hero-h">Key insights</div>{hl}</div>')
    else:
        hero = ""

    # Editor's take removed — an LLM-written banner conflicts with the deterministic
    # positioning; `take` is retained in the signature but no longer rendered.

    # ---- today's brief: level-1 scannable counts (all real, from this build) ----
    def _bm(v, singular, hot=False, note=""):
        cls = "brief-v hot" if (hot and v) else "brief-v"
        if v == 1:
            label = singular
        elif singular.endswith("y"):
            label = singular[:-1] + "ies"                # study -> studies
        else:
            label = singular + "s"
        label = label[0].upper() + label[1:] if label else label
        note_html = f'<div class="brief-note">{note}</div>' if note else ""
        return (f'<div class="brief-m"><div class="{cls}">{v}</div>'
                f'<div class="brief-l">{label}</div>{note_html}</div>')
    _totp = [sum(h["layers"].values()) for h in prior if isinstance(h.get("layers"), dict) and h.get("layers")]
    _updnote = f"typical ~{sum(_totp)/len(_totp):.0f}" if len(_totp) >= 3 else ""
    metrics = ('<div class="brief">'
               + _bm(len(items), "update", note=_updnote)
               + _bm(o["layers"].get("regulation", 0), "regulatory<br>action", hot=True)
               + _bm(len(o["coverage_actions"]), "coverage<br>decision", hot=True)
               + _bm(o["layers"].get("clinical", 0), "clinical<br>study")
               + '</div>')
    cta = '<button class="cta" data-goto="feed">Browse all evidence \u2192</button>'

    # ---- top updates: the five highest-ranked headlines, scannable ----
    _ranked = sorted(items, key=lambda i: -rank_score(i)[0])[:5]
    if _ranked:
        _rows = "".join(
            f'<a class="tbrow" href="{safe_url(i["url"])}" target="_blank" rel="noopener">'
            f'<span class="tbn">{n}</span>'
            f'<span class="tbc"><span class="tbt">{html.escape(i["title"])}</span>'
            f'<span class="tbs">{html.escape(i["source"])} · {i["date"] or "date unknown"}</span></span></a>'
            for n, i in enumerate(_ranked, 1))
        top_updates = ('<div class="sec">Top updates</div>'
                       '<div class="seccap">Ranked automatically by transparent rule. <span class="lnk" data-goto="sources">How ranking works</span></div>'
                       f'<div class="tbrief">{_rows}</div>')
    else:
        top_updates = ""

    # ---- trending topics: tracked terms gaining mentions vs their recent baseline ----
    trending = ""
    if history and len(history) >= 4:
        _tod = history[-1].get("terms", {})
        _pri = history[:-1]
        _mv = []
        for _t, _now in _tod.items():
            if _now <= 0:
                continue
            _base = [h.get("terms", {}).get(_t, 0) for h in _pri[-28:]]
            _avg = sum(_base) / len(_base) if _base else 0
            _pct = 100.0 if _avg == 0 else ((_now - _avg) / _avg) * 100
            if _pct > 0:
                _mv.append((_pct, _t, _now))
        _mv.sort(key=lambda r: -r[0])
        if _mv:
            _chips = "".join(
                f'<button class="tchip" data-term="{html.escape(_t)}">'
                f'{html.escape(TERM_DISPLAY.get(_t, _t))}<span class="tchip-n">{_now}</span></button>'
                for _pct, _t, _now in _mv[:6])
            trending = ('<div class="sec">Trending topics</div>'
                        '<div class="seccap">Terms with the largest increase vs their 28-day baseline. Select one to see the updates behind it.</div>'
                        f'<div class="tchips">{_chips}</div>')

    popular = popular_topics_html(items)
    home = f'''<div class="sec nomt">Today’s Brief</div>
<div class="homedisc">Automated aggregation from public sources using transparent rules. For research only. <span class="lnk" data-goto="sources">Methodology</span></div>
{metrics}
<div class="briefing">
{topstory}
{hero}
</div>
<div class="jstrip-h">Today’s evidence journey <span class="jstrip-sep">·</span> <span class="lnk" data-goto="analysis">See Analysis →</span></div>
{journey_strip}
{top_updates}
{trending}
{popular}
{cov_mini}
<div class="homecta">{cta}</div>'''
    analysis = f'''<div class="sec nomt">Current evidence landscape</div>
<div class="seccap">Where activity concentrates in the current build — by lifecycle stage, region, regulator, specialty and coverage route.</div>
{journey_html}
<div class="panels" style="margin-top:10px">{geo_panel}{regulators_panel}</div>
<div class="panels" style="margin-top:8px">{payers_panel}{clinfocus}</div>
<div style="margin-top:8px">{pathway}</div>
<div class="sec">Commercial pathway</div>
<div class="seccap">From evidence to adoption — early signals followed by the two commercial gates.</div>
<div class="subh">Early signals <span class="subh-n">Economic-endpoint trials and HTA studies</span></div>
<div class="tiles g2">{ind_html}</div>
<div class="subh" style="margin-top:12px">Commercial gates</div>
<div class="tiles g2">{gate_html}</div>'''
    return home, analysis


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


def _vol_level(now, lo, hi, n):
    if hi == lo:
        return "in line with its recent range"
    if now == hi and n >= 5:
        return f"the highest across the last {n} builds"
    pos = (now - lo) / (hi - lo)
    if pos >= 0.75:
        return "elevated versus its recent range"
    if pos <= 0.25:
        return "quiet versus its recent range"
    return "typical for its recent range"


def trends_html(items, history):
    """Trends TAB: top trend (count-led) + build volume + biggest term shifts (classified) + orgs."""
    if not history:
        return ''   # first-build notice is surfaced at the top of the Analysis view instead
    today = history[-1]
    prior = history[:-1]

    movers = []
    if len(prior) >= 3:
        for term, now in today.get("terms", {}).items():
            base = [h["terms"].get(term, 0) for h in prior[-28:]]
            avg = sum(base) / len(base) if base else 0
            if now == 0 and avg < 0.5:
                continue
            pct = (100 if now else 0) if avg == 0 else ((now - avg) / avg) * 100
            movers.append((pct, term, now, avg))
        movers.sort(key=lambda r: -r[0])

    # highlight the single top mover as a card (leads the Trending topics section)
    highlight = ""
    if movers and movers[0][0] > 0:
        pct, term, now, avg = movers[0]
        avg_txt = f"~{avg:.0f}" if avg >= 1 else "under 1"
        cls = TERM_CLASS.get(term, "")
        cls_tag = f'<span class="tclass">{html.escape(cls)}</span>' if cls else ""
        highlight = (f'<div class="tmcard"><div class="tmcard-l">Top mover</div>'
                     f'<div class="tmcard-t">{html.escape(TERM_DISPLAY.get(term, term))} {cls_tag}</div>'
                     f'<div class="tmcard-s">{now} in this build · {avg_txt} typical · '
                     f'<b class="tmcard-pct">{"+" if pct >= 0 else ""}{pct:.0f}%</b></div></div>')

    # the rest of the movers, listed below the highlight
    if len(prior) >= 3 and movers and movers[0][0] > 0:
        peak = max((abs(r[0]) for r in movers), default=1) or 1
        bars = ""
        for r in movers[1:7]:
            pct, term, now, avg = r
            cls = TERM_CLASS.get(term, "")
            cls_html = f'<span class="tclass">{html.escape(cls)}</span>' if cls else ""
            if now == 0:
                bl = f"~{avg:.0f}" if avg >= 1 else "under 1"
                bars += (f'<div class="trow"><div class="tn dim">'
                         f'<span class="tnm">{html.escape(TERM_DISPLAY.get(term, term))}</span>{cls_html}</div>'
                         f'<div class="tzero">no mentions this build (typical {bl}/build)</div></div>')
                continue
            up = pct >= 0
            bars += (f'<div class="trow"><div class="tn{"" if up else " dim"}">'
                     f'<span class="tnm">{html.escape(TERM_DISPLAY.get(term, term))}</span>{cls_html}</div>'
                     f'<div class="tb"><div class="tf{"" if up else " down"}" style="width:{min(abs(pct) / peak * 100, 100):.0f}%"></div></div>'
                     f'<div class="tp{"" if up else " dim"}">{"+" if up else ""}{pct:.0f}%</div>'
                     f'<div class="tcount">{now} vs {("~%.0f" % avg) if avg >= 1 else "under 1"}</div></div>')
        rest = f'<div class="panel" style="margin-top:10px">{bars}</div>' if bars else ""
        trending = ('<div class="sec">Trending topics</div>'
                    '<div class="seccap">Terms with the largest increase vs their 28-day baseline. Small bases can produce large percentage changes.</div>'
                    f'{highlight}{rest}')
    else:
        need = max(4 - len(history), 1)
        trending = ('<div class="sec">Trending topics</div>'
                    f'<div class="seccap">Accruing — term trends need a few days of history. ~{need} more to go.</div>')

    return trending
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
    <a href="https://github.com/asarmah123/ai-health-evidence-monitor/blob/main/TAXONOMY.md" target="_blank" rel="noopener">TAXONOMY.md</a> —
    provisional, regional and code-only statuses are counted separately and never merged into a median.</div>
</div>
{evidence_panel}'''


# ------------------------------------------------------------------- render
CSS = """
:root{color-scheme:light;--line:#e8e8e8;--mute:#767676;--ink:#1a1a1a;--accent:#9c2c2c}
*{box-sizing:border-box}
body{margin:0;padding:26px 20px 60px;background:#fff;color:var(--ink);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:28px;margin:0 0 3px;letter-spacing:-.022em;font-weight:700}
.tagline{font-size:17.5px;font-weight:500;color:#232323;margin:4px 0 10px;line-height:1.35;letter-spacing:-.008em}
.forwhom{font-size:12.5px;color:#707070;margin:0 0 9px}
.sub{color:#9a9a9a;font-size:12px;margin:0 0 16px;font-variant-numeric:tabular-nums}
/* tabs */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:20px;
 position:sticky;top:0;background:#fff;z-index:10;padding-top:2px}
.tab{font-size:14px;padding:9px 16px;color:#6a6a6a;cursor:pointer;border-bottom:2px solid transparent;border-radius:6px 6px 0 0;
 white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--ink);font-weight:650;border-bottom:2px solid var(--accent);background:#fbf6f6}
.view{display:none}.view.on{display:block}
.sec{font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#2f2f2f;
 margin:40px 0 12px;display:flex;align-items:center;gap:10px}
.sec:first-child{margin-top:6px}
.seeall{font-size:10.5px;font-weight:600;letter-spacing:0;text-transform:none;color:var(--accent);
 cursor:pointer;text-decoration:none}
.seeall:hover{text-decoration:underline}
/* tiles */
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tiles.g2{grid-template-columns:repeat(2,1fr)}
.seccap{font-size:13.5px;color:#565656;margin:-2px 0 14px;line-height:1.6}
.tile{border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.tl{font-size:11.5px;color:#565656;text-transform:uppercase;letter-spacing:.05em}
.tv{font-size:24px;font-weight:700;margin-top:4px}
.ts{font-size:11.5px;color:#666;margin-top:3px;line-height:1.4}
/* digest */
.digboxes{display:flex;flex-direction:column;gap:16px}
.digbox{border:1px solid #d6d6d6;border-radius:12px;overflow:hidden;background:#fff}
.digbox-h{display:flex;align-items:center;font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#565656;background:#f2f2f0;padding:11px 16px;cursor:pointer;list-style:none}
.digbox-h::-webkit-details-marker{display:none}
.digbox-h::after{content:"▸";margin-left:auto;color:#a8a8a8;font-size:11px;font-weight:400}
.digbox[open]>.digbox-h::after{content:"▾"}
.digbox[open]>.digbox-h{border-bottom:1px solid #e6e6e6}
.digbox-h:hover{background:#ecece9}
.digbox-n{color:#9a9a9a;font-weight:600;margin-left:6px}
.digwhy{font-size:12.5px;font-weight:400;font-style:italic;text-transform:none;letter-spacing:normal;color:#606060;padding:10px 16px 2px;line-height:1.5}.digwhy b{color:#3d3d3d;font-style:normal}
.dig{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:baseline;
 padding:13px 16px;text-decoration:none;color:var(--ink);border-bottom:1px solid #eee}
.dig:last-child{border-bottom:none}
.dig:hover{background:#fafafa}
.dttl{font-size:15.5px;font-weight:600;line-height:1.4;color:var(--ink)}
.dsrc{font-size:11.5px;color:#767676;white-space:nowrap}
.dnote{border:1px dashed var(--line);border-radius:9px;padding:16px;font-size:12.5px;color:#8a8a8a}
.abt{font-size:14.5px;color:#333;line-height:1.68;max-width:70ch;margin:-2px 0 14px}
.abt.scope{font-size:13px;color:#8a8a8a}
.abt a,.lnk{color:#1f6feb;text-decoration:none;cursor:pointer;border-bottom:1px solid rgba(31,111,235,.35)}
.abt a:hover,.lnk:hover{border-bottom-color:#1f6feb}
.tbrief{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:-2px 0 20px}
.tbrow{display:flex;gap:12px;align-items:baseline;padding:11px 14px;text-decoration:none;border-top:1px solid var(--line)}
.tbrow:first-child{border-top:none}
.tbrow:hover{background:#f7f8fa}
.tbn{flex:none;font-size:12px;font-weight:700;color:#b3b3b3;width:16px;text-align:right}
.tbc{display:flex;flex-direction:column;gap:2px;min-width:0}
.tbt{font-size:14.5px;font-weight:600;color:#1a1a1a;line-height:1.4}
.tbrow:hover .tbt{color:#1f6feb}
.tbs{font-size:11.5px;color:#9a9a9a}
.tchips{display:flex;flex-wrap:wrap;gap:8px;margin:-2px 0 20px}
.tchip{display:inline-flex;align-items:center;gap:7px;font:inherit;font-size:13px;color:#333;background:#f4f6f8;border:1px solid var(--line);border-radius:16px;padding:6px 12px;cursor:pointer}
.tchip:hover{background:#eaf1fb;border-color:#c7dbfa;color:#1f6feb}
.tchip-n{font-size:11px;font-weight:700;color:#8a8a8a;background:#fff;border-radius:9px;padding:0 6px}
.tchip:hover .tchip-n{color:#1f6feb}
/* Follow topics */
.tpc-strip{display:flex;flex-wrap:wrap;gap:8px;margin:-2px 0 20px}
.tpc-strip .tpc{background:#f4f6f8;border:1px solid var(--line);border-radius:16px;padding:4px 6px 4px 10px}
.tpc-lib{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 22px;margin:2px 0 8px}
.tpc-grp{min-width:0}
.tpc-h{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#8a8a8a;margin:2px 0 6px}
.tpc{display:inline-flex;align-items:center;gap:7px;max-width:100%}
.tpc-lib .tpc{display:flex;padding:5px 2px;border-bottom:1px solid #f0f0f0}
.tpc-star{font:inherit;font-size:14px;line-height:1;color:#c2b36a;background:none;border:none;cursor:pointer;padding:0 2px;flex:none}
.tpc-star.on{color:#c99a2e}
.tpc-l{font-size:13.5px;color:#333;cursor:pointer;flex:1 1 auto;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tpc-l:hover{color:#1f6feb}
.tpc-n{font-size:11px;font-weight:700;color:#9a9a9a}
.tpc-zero{opacity:.62}
.tpc-rss{font-size:9.5px;font-weight:700;letter-spacing:.04em;color:#9a7a7a;background:#f6efef;border:1px solid #ecdede;border-radius:4px;padding:1px 5px;text-decoration:none;flex:none}
.tpc-rss:hover{color:#9c2c2c;border-color:#e0c9c9}
#your-topics{margin:0 0 4px}
.tpc-your-h{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#8a8a8a;margin:0 0 6px}
.tpc-your{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}
.tpc-chip{font-size:12.5px;color:#333;background:#eef3fb;border:1px solid #d5e3f7;border-radius:14px;padding:4px 11px;cursor:pointer}
.tpc-chip:hover{color:#1f6feb;border-color:#b9d2f2}
@media(max-width:640px){.tpc-lib{grid-template-columns:1fr}}
.homecta{text-align:center;margin:26px 0 4px}
.tmcard{background:#faf6f6;border:1px solid #ecdede;border-left:3px solid #9c2c2c;border-radius:0 10px 10px 0;padding:12px 15px;margin:2px 0 0}
.tmcard-l{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#9c2c2c;margin-bottom:4px}
.tmcard-t{font-size:19px;font-weight:700;color:#1a1a1a;line-height:1.2;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tmcard-s{font-size:13px;color:#5f5f5f;margin-top:5px}
.tmcard-pct{color:#9c2c2c}
.subh-n{font-weight:400;text-transform:none;letter-spacing:0;color:#9a9a9a;font-size:12px;margin-left:6px}
.faq{margin:-2px 0 20px}
.faqi{border:1px solid var(--line);border-radius:9px;padding:11px 14px;margin-bottom:8px;font-size:13.5px;color:#3a3a3a;line-height:1.62}
.faqi summary{font-weight:600;color:#1a1a1a;cursor:pointer;font-size:14px}
.faqi[open] summary{margin-bottom:7px}
.faqi a{color:#1f6feb;text-decoration:none;border-bottom:1px solid rgba(31,111,235,.35)}
/* panels */
.panels{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.panel,.spark{border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.ph{font-size:13.5px;font-weight:680}
.psub{font-size:11.5px;color:#666;margin-bottom:10px}
.spark svg{width:100%;height:52px;display:block;margin-top:6px}
.sparl{font-size:10.5px;color:#a5a5a5;margin-top:6px}
.split{height:9px;border-radius:5px;background:#dbe6d9;overflow:hidden;margin:4px 0 6px}
.sfill{height:9px;background:#c9d8ee}
.slab{display:flex;justify-content:space-between;font-size:11px;color:#666}
.trow{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.tn{font-size:13px;width:150px;flex:none}.tn.dim,.tp.dim{color:#a0a0a0}
.tb{flex:1;height:6px;background:#f2f2f2;border-radius:3px}
.tf{height:6px;background:var(--accent);border-radius:3px;opacity:.75}.tf.down{background:#c4c4c4}
.tp{font-size:11.5px;font-weight:600;color:var(--accent);width:40px;text-align:right}
.tcount{font-size:10.5px;color:#9a9a9a;width:66px;text-align:right;white-space:nowrap;flex:none}
.tzero{flex:1;text-align:right;font-size:12px;color:#a5a5a5;white-space:nowrap}
.tnm{display:block}
.tclass{display:block;font-size:9px;color:#adadad;text-transform:uppercase;letter-spacing:.02em;margin-top:1px}
.tclass-lg{font-size:11px;color:#8a8a8a;text-transform:uppercase;letter-spacing:.04em;margin-top:5px}
.newmini{font-size:8.5px;font-weight:650;text-transform:uppercase;color:#1f8a70;background:#eaf6f1;padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:middle}
.volnow{font-size:14px;color:var(--ink);margin:3px 0 4px}.volnow b{font-weight:680}
.pipeline{margin:2px 0 6px}
.pstep{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.pstep-n{flex:none;width:24px;height:24px;border-radius:50%;background:var(--accent);color:#fff;font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center}
.pstep-t{font-size:13.5px;font-weight:680;color:var(--ink)}
.pstep-d{font-size:12.5px;color:#5a5a5a;line-height:1.5;margin-top:3px}
.parrow{text-align:center;color:#cbb6b6;font-size:14px;line-height:1;margin:3px 0}
.principles{margin:0 0 6px;padding-left:20px}
.principles li{font-size:13.5px;color:#3f3f3f;line-height:1.55;margin-bottom:7px}.principles b{color:var(--ink)}
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
.grp-h{font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#7a7a7a;margin-bottom:8px}
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
h3{font-size:16px;margin:0 0 6px;font-weight:600;line-height:1.4}
h3 a{color:var(--ink);text-decoration:none}h3 a:hover{text-decoration:underline}
.summ{font-size:13.5px;color:#3f3f3f;margin-bottom:9px}
.acts{margin-top:9px}
.acts button{background:none;border:none;padding:0;font-size:12px;color:var(--mute);cursor:pointer}
.acts button:hover{color:var(--ink);text-decoration:underline}
/* sources */
.hubs{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:8px}
.hub{border:1px solid var(--line);border-radius:8px;padding:10px 12px;text-decoration:none;display:block}
.hub:hover{border-color:#bfbfbf;background:#fafafa}
.hub .n{font-size:13px;font-weight:600;color:var(--ink)}.hub .d{font-size:11.5px;color:var(--mute);margin-top:2px}
.foot{font-size:11.5px;color:#a5a5a5;margin-top:22px;border-top:1px solid var(--line);padding-top:12px}
.disc{font-size:11.5px;color:#7a5f14;background:#fdf8e6;border:1px solid #e0cd8a;border-left:3px solid #c9a227;border-radius:8px;padding:9px 13px;margin:0 0 16px;line-height:1.45}
.pagefoot{font-size:11.5px;color:#8a8a8a;line-height:1.6;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
.pagefoot b{color:#5f5f5f}
.pagefoot-s{margin-top:9px;color:#b0b0b0;font-variant-numeric:tabular-nums}
.discmore{display:inline;margin-left:0}
.fdot{color:#c7c7c7}
.jstrip-sep{color:#c7c7c7}
.discmore>summary{display:inline;cursor:pointer;color:var(--accent);list-style:none}
.discmore>summary::-webkit-details-marker{display:none}
.discmore>summary:hover{text-decoration:underline}
.discmore[open]{display:block;margin:6px 0 0}
/* today's biggest development — dominant top card */
.topstory{border:1px solid #dcc9c9;border-left:4px solid var(--accent);background:linear-gradient(180deg,#fdf7f7,#fff);
 border-radius:12px;padding:18px 20px;margin:8px 0 12px}
.topstory-l{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:8px}
.topstory-t{display:inline-block;font-size:19px;font-weight:700;line-height:1.3;color:var(--ink);text-decoration:none;letter-spacing:-.01em}
.topstory-t:hover{text-decoration:underline}
.topstory-t2{font-size:19px;font-weight:680;color:#8a8a8a}
.topstory-m{font-size:12px;color:var(--mute);margin-top:8px;line-height:1.5}
.topstory-why{font-size:13px;font-style:italic;color:#4a4a4a;margin-top:9px;line-height:1.5}
.topstory-why b{color:var(--ink)}
.topstory.quiet{border-left-color:#c4c4c4;background:#fafafa}
.newflag{font-size:9.5px;font-weight:650;text-transform:uppercase;letter-spacing:.03em;color:#1f8a70;background:#eaf6f1;padding:2px 7px;border-radius:4px;margin-left:10px;vertical-align:middle}
.activestrip{background:#fbf6f6;border:1px solid #e6d9d9;border-radius:9px;padding:10px 13px;margin-bottom:16px;font-size:13px;color:#4a4a4a;line-height:1.5}
.as-l{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);margin-right:10px}
.activestrip b{color:var(--ink)}
/* collapsible detailed analytics */
.more{margin-top:10px;border-top:1px solid var(--line);padding-top:4px}
.more>summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--accent);padding:10px 0;list-style:none}
.more>summary::-webkit-details-marker{display:none}
.more>summary::before{content:"▸  ";color:var(--accent)}
.more[open]>summary::before{content:"▾  "}
.more>summary:hover{text-decoration:underline}
/* collapsible main overview sections (header = toggle) */
.ovsec{margin:0}
.secsum{font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#2f2f2f;
 margin:28px 0 11px;display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none}
.secsum::-webkit-details-marker{display:none}
.secsum::before{content:"▾";color:#c4c4c4;font-size:10px;font-weight:400}
.ovsec:not([open])>.secsum::before{content:"▸"}
.ovsec:not([open])>.secsum{margin-bottom:6px}
/* methodology list */
.method{margin:0 0 10px;padding-left:22px}
.method li{font-size:13.5px;color:#3f3f3f;line-height:1.55;margin-bottom:8px}
.method b{color:var(--ink)}
.method-note{font-size:12.5px;color:#5a5a5a;line-height:1.6;margin:2px 0 8px;padding:11px 13px;background:#fafafa;border:1px solid var(--line);border-radius:9px}
.method-note b{color:var(--ink)}
/* feed sort + geography chip + why-ranked */
.sortl{font-size:12px;color:#7a7a7a;display:inline-flex;align-items:center;gap:5px}
.sortsel{font-size:12.5px;padding:5px 8px;border:1px solid #dcdcdc;border-radius:7px;background:#fff;color:var(--ink);font-family:inherit;cursor:pointer}
.geo{font-size:10.5px;font-weight:600;letter-spacing:.03em;color:#2f6f9f;background:#eef4fa;padding:2px 7px;border-radius:4px}
.whyrank{display:inline-block;margin-left:14px}
.whyrank>summary{cursor:pointer;font-size:12px;color:var(--mute);list-style:none}
.whyrank>summary::-webkit-details-marker{display:none}
.whyrank>summary:hover{color:var(--ink);text-decoration:underline}
.whyrank ul{margin:7px 0 2px;padding-left:18px}
.whyrank li{font-size:12px;color:#5a5a5a;line-height:1.5}
/* mobile refinements */
@media(max-width:640px){
  .topstory-t,.topstory-t2{font-size:17px}
  .fbar{gap:8px}.sortl{width:100%;justify-content:space-between}
  .tab{padding:11px 14px}
  button.f{padding:8px 14px}
  .acts{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
  .whyrank{margin-left:0}
}
.take{border:1px solid #d8d8d8;border-left:3px solid var(--accent);border-radius:8px;
 padding:12px 15px;margin-bottom:18px;background:#fbfaf9}
.take-l{font-size:9.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:4px}
.take-t{font-size:15px;line-height:1.6;color:#2a2a2a}
.hero{padding:2px 0 0;margin-bottom:14px}
.hero-h{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#3d3d3d;margin-bottom:8px}
.hero-n{color:#9a8a8a;font-weight:600;margin-left:6px}
.hero-line{font-size:15px;color:#2a2a2a;line-height:1.6;padding:3px 0}
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
.cat-t{font-size:15px;font-weight:620;display:flex;align-items:baseline;gap:6px}
.cat-n{font-size:11.5px;color:#9a9a9a;font-weight:500;margin-left:auto}
.cat-d{font-size:13px;color:#5a5a5a;margin-top:6px;line-height:1.5}
.catback{font-size:12px;color:#777;cursor:pointer;margin-bottom:12px;display:inline-block}
.catback:hover{color:var(--ink)}
.cat-head{font-size:18px;font-weight:650;margin:0 0 5px;letter-spacing:-.01em}
.cat-lead{font-size:12.5px;color:#666;line-height:1.5;max-width:660px}
@media(max-width:640px){.catgrid{grid-template-columns:1fr}}
/* today's brief — level-1 scannable summary strip */
.brief{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:8px 0;margin:4px 0 16px}
.brief-m{text-align:center;padding:0 6px;border-right:1px solid #efe6e6}
.brief-m:last-child{border-right:none}
.brief-v{font-size:24px;font-weight:700;line-height:1;color:var(--ink);font-variant-numeric:tabular-nums}
.brief-v.hot{color:var(--accent)}
.brief-l{font-size:11.5px;color:#666;margin-top:7px;line-height:1.3;text-align:center}
.sec.nomt{margin-top:0}
.homedisc{font-size:12px;color:#8a8a8a;line-height:1.5;margin:-4px 0 14px}
.firstrun{font-size:13px;color:#4a6a8a;background:#f2f7fb;border:1px solid #d7e6f2;border-radius:8px;padding:9px 13px;margin:0 0 14px}
.jstrip-h{font-size:11.5px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:#8a8a8a;margin:18px 0 9px}
.jstrip-h .lnk{text-transform:none;letter-spacing:0;font-weight:500}
.jstrip{display:flex;align-items:center;flex-wrap:wrap;gap:4px;margin:0 0 20px}
.jnode{display:inline-flex;align-items:center;gap:7px;font:inherit;font-size:12.5px;color:#555;background:none;border:none;padding:5px 4px;cursor:pointer}
.jnode:hover .jnl{color:#1f6feb}
.jdot{width:11px;height:11px;border-radius:50%;border:1.5px solid #cfcfcf;background:#fff;flex:none}
.jdot.on{border:none}
.jnl{white-space:nowrap}
.jsep{color:#c7c7c7;font-size:13px;flex:none}
/* feed search */
.searchbar{margin:2px 0 18px}
.search{width:100%;font-size:14.5px;padding:11px 14px;border:1px solid #d5d5d5;border-radius:9px;
 background:#fff;color:var(--ink);font-family:inherit}
.search:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px #f3e3e3}
.search::placeholder{color:#9a9a9a}
@media(max-width:640px){.tiles,.cov-grid{grid-template-columns:repeat(2,1fr)}.panels{grid-template-columns:1fr}
 .dig{grid-template-columns:1fr;gap:3px}.dsrc{white-space:normal}
 .brief{grid-template-columns:repeat(2,1fr);gap:10px 4px}.brief-m{border-right:none}.brief-v{font-size:22px}}
/* acronym tooltips */
abbr[title]{text-decoration:underline dotted;text-decoration-color:#c9b3b3;text-underline-offset:2px;cursor:help}
.newmini.lowbase{background:#eef0f2;color:#8a8a8a}
.bh{display:flex;flex-wrap:wrap;gap:10px;margin:2px 0 10px}
.bh-m{flex:1;min-width:130px;border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.bh-v{font-size:21px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums;line-height:1.1}
.bh-l{font-size:11px;color:#6a6a6a;margin-top:4px;line-height:1.3}
/* evidence journey — vertical stepped flow */
.journey{margin:2px 0 10px}
.jstep{display:flex;justify-content:space-between;align-items:center;border:1px solid var(--line);border-radius:9px;padding:12px 15px;cursor:pointer;background:#fff;transition:background .12s}
.jstep:hover{border-color:#bcbcbc;background:#fafafa}
.jstep:focus-visible,.cat:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.jlabel{font-size:14.5px;font-weight:600;color:var(--ink)}
.jval{font-size:17px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums;text-align:right}
.jnote{font-size:10.5px;font-weight:400;color:#8a8a8a;margin-top:2px;letter-spacing:0}
.brief-note{font-size:10px;color:#9a9a9a;margin-top:5px;line-height:1.2;text-transform:none;letter-spacing:0}
.jarrow{height:15px;margin:0;font-size:0;background:linear-gradient(#cfcfcf,#cfcfcf) no-repeat center/2px 100%}
/* primary call-to-action */
.cta{display:inline-block;background:var(--accent);color:#fff;font-size:14.5px;font-weight:600;border:none;border-radius:8px;padding:12px 22px;cursor:pointer;margin:0 0 6px;letter-spacing:.01em}
.cta:hover{background:#822525}
/* stronger top-story emphasis + more breathing room */
.topstory-t,.topstory-t2{font-size:23px}
@media(max-width:640px){.topstory-t,.topstory-t2{font-size:19px}.cta{display:block;width:100%;text-align:center}}
.editorial{font-size:14px;color:#4a4a4a;line-height:1.55;margin:2px 0 12px;padding-left:11px;border-left:3px solid var(--line)}
.topstory[data-open]{cursor:pointer}
.ts-kind{font-weight:650;color:#4a4a4a}
.ts-src{color:#4a4a4a}.ts-date{color:var(--mute)}
.briefing-h{font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#2f2f2f;margin:6px 0 12px}
.briefing{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}
"""

JS = """
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let tier='all', layer='all', hideRead=false, q='', sort='importance';
let region='all', stype='all', dwin='all', topic='all';
function withinDays(d,n){ if(!d) return false; const t=Date.parse(d); if(isNaN(t)) return false; return (Date.now()-t) <= (n+1)*864e5; }
function updateClear(){ const b=document.getElementById('fclear'); if(b) b.style.display=(region!=='all'||stype!=='all'||dwin!=='all'||topic!=='all')?'':'none'; }
const FKEY='aiheor_follow_v1';
function followed(){ try{return JSON.parse(localStorage.getItem(FKEY)||'[]')}catch(e){return[]} }
function toggleFollow(slug){ let a=followed(); a=a.includes(slug)?a.filter(x=>x!==slug):a.concat([slug]); try{localStorage.setItem(FKEY,JSON.stringify(a))}catch(e){} renderFollowState(); }
function renderFollowState(){ const f=followed();
  $$('.tpc-star').forEach(b=>{const on=f.includes(b.dataset.follow); b.textContent=on?'★':'☆'; b.classList.toggle('on',on);});
  const box=$('#your-topics'); if(box){ box.innerHTML = f.length
    ? ('<div class="tpc-your-h">Your topics</div><div class="tpc-your">'+f.map(s=>`<span class="tpc-chip" data-topic="${s}" role="button" tabindex="0">${esc((TOPIC_LABELS&&TOPIC_LABELS[s])||s)}</span>`).join('')+'</div>')
    : ''; } }
const KEY='aiheor_read_v1';
const read=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
const save=()=>localStorage.setItem(KEY,JSON.stringify([...read]));
const LABEL={daily:'Daily',weekly:'Weekly',monthly:'Monthly'};
const SC={research:'#6a4c93',clinical:'#9c2c44',regulation:'#2f6f9f',heor:'#1f8a70',access:'#b0842b',industry:'#64748b'};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const safeUrl=u=>(/^https?:\/\//i.test(u||'')?u:'#');

// tab switching
function goto(name){
  $$('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab===name));
  $$('.view').forEach(v=>v.classList.toggle('on',v.id==='view-'+name));
  if(name==='feed'){ showDir(); }   // always land on the category directory
  window.scrollTo(0,0);
}
function showDir(){ const qq=$('#q'); if(qq) qq.value=''; q=''; topic='all'; updateClear(); $('#feed-dir').style.display='block'; $('#feed-list').style.display='none'; }
function showList(){ $('#feed-dir').style.display='none'; $('#feed-list').style.display='block'; window.scrollTo(0,0); }
function goToTopic(slug){ topic=slug; layer='all'; q=''; const qq=$('#q'); if(qq)qq.value='';
  $$('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab==='feed'));
  $$('.view').forEach(v=>v.classList.toggle('on',v.id==='view-feed'));
  const T=(TOPIC_LABELS&&TOPIC_LABELS[slug])||slug;
  const h=$('#cat-head'),l=$('#cat-lead');
  if(h)h.textContent=T; if(l)l.textContent='Following “'+T+'” — items in this build tagged to this topic.';
  updateClear(); showList(); render(); window.scrollTo(0,0);
  try{history.replaceState(null,'','?topic='+encodeURIComponent(slug));}catch(e){}
}
$$('.tab').forEach(t=>t.onclick=()=>goto(t.dataset.tab));
document.addEventListener('click',e=>{
  const g=e.target.closest('[data-goto]'); if(g){e.preventDefault();goto(g.dataset.goto);
    if(g.dataset.layer){layer=g.dataset.layer;
      const h=$('#cat-head'),l=$('#cat-lead');
      if(h)h.textContent=g.dataset.label||'Updates';
      if(l)l.textContent=g.dataset.desc||'';
      showList();render();}
    if(g.dataset.anchor){ const a=$('#'+g.dataset.anchor); if(a) a.scrollIntoView({block:'start'}); }
  }
});
document.addEventListener('click',e=>{
  const c=e.target.closest('.topstory[data-open]');
  if(c && !e.target.closest('a')){ const u=c.dataset.open; if(u && u!=='#') window.open(u,'_blank','noopener'); }
});
document.addEventListener('click',e=>{
  const st=e.target.closest('.tpc-star'); if(st){ e.preventDefault(); toggleFollow(st.dataset.follow); return; }
  const tp=e.target.closest('[data-topic]'); if(tp){ e.preventDefault(); goToTopic(tp.dataset.topic); }
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Enter'&&e.key!==' ') return;
  const t=e.target.closest('.jstep,.cat,.tpc-l,.tpc-chip'); if(t){e.preventDefault();t.click();}
});

// feed
function render(){
  const list=ITEMS.filter(i=>tier==='all'||i.tier===tier)
                  .filter(i=>layer==='all'||i.layer===layer)
                  .filter(i=>topic==='all'||(i.topics||[]).includes(topic))
                  .filter(i=>region==='all'||i.region===region)
                  .filter(i=>stype==='all'||i.stype===stype)
                  .filter(i=>dwin==='all'||withinDays(i.date,+dwin))
                  .filter(i=>!(hideRead&&read.has(i.id)))
                  .filter(i=>!q||((i.title+' '+i.source+' '+(i.summary||'')).toLowerCase().includes(q)));
  const byDateDesc=(a,b)=>(b.date||'').localeCompare(a.date||'');
  const cmp={
    importance:(a,b)=>((b.score||0)-(a.score||0))||byDateDesc(a,b),
    newest:byDateDesc,
    geography:(a,b)=>(a.country||'zzz').localeCompare(b.country||'zzz')||((b.score||0)-(a.score||0)),
    source:(a,b)=>a.source.localeCompare(b.source)||byDateDesc(a,b),
  }[sort]||((a,b)=>(b.score||0)-(a.score||0));
  list.sort(cmp);
  $('#feed').innerHTML = list.map(i=>`
    <div class="card ${read.has(i.id)?'read':''}" style="border-left:3px solid ${SC[i.layer]||'#dcdcdc'}">
      <div class="meta"><span class="tag ${i.tier}">${LABEL[i.tier]}</span>
        <span class="src">${esc(i.source)} · ${i.date||'date unknown'}</span>
        ${i.country?`<span class="geo">${esc(i.country)}</span>`:''}</div>
      <h3><a href="${esc(safeUrl(i.url))}" target="_blank" rel="noopener">${esc(i.title)}</a></h3>
      ${i.summary?`<div class="summ">${esc(i.summary)}</div>`:''}
      <div class="acts">
        <button data-i="${i.id}">${read.has(i.id)?'Mark unread':'Mark read'}</button>
        ${(i.why&&i.why.length)?`<details class="whyrank"><summary>Why ranked · ${i.score}</summary><ul>${i.why.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}
      </div>
    </div>`).join('') || '<div class="dnote">Nothing matches — try another filter.</div>';
  $$('.acts button').forEach(b=>b.onclick=()=>{const id=b.dataset.i;read.has(id)?read.delete(id):read.add(id);save();render();});
  const baseN=ITEMS.filter(i=>tier==='all'||i.tier===tier)
                   .filter(i=>layer==='all'||i.layer===layer)
                   .filter(i=>!(hideRead&&read.has(i.id))).length;
  const fp=[];
  if(region!=='all')fp.push(esc(region));
  if(stype!=='all')fp.push(esc(stype));
  if(dwin!=='all')fp.push('last '+dwin+' days');
  if(q)fp.push('“'+esc(q)+'”');
  $('#count').innerHTML = fp.length
    ? `<b>Showing ${list.length} of ${baseN}</b> · ${fp.join(' · ')} · ${read.size} read`
    : `${list.length} update${list.length===1?'':'s'} · ${read.size} read`;
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
  $('#cat-head').textContent='All updates';
  $('#cat-lead').textContent='Every source across all six categories, unfiltered.';
  showList(); render();};
const back=$('[data-back]');
if(back) back.onclick=()=>showDir();
$('#hide').onclick=e=>{hideRead=!hideRead;e.target.classList.toggle('on',hideRead);render();};
const sortSel=$('#sort');
if(sortSel) sortSel.onchange=()=>{sort=sortSel.value;render();};
function applyFilter(){ updateClear();
  if($('#feed-list').style.display==='none'){ layer='all';
    const h=$('#cat-head'),l=$('#cat-lead');
    if(h) h.textContent='Filtered updates';
    if(l) l.textContent='Matching items across every category, in the latest build.';
    showList(); }
  render(); }
const fRegion=$('#fregion'); if(fRegion) fRegion.onchange=()=>{region=fRegion.value;applyFilter();};
const fStype=$('#fstype'); if(fStype) fStype.onchange=()=>{stype=fStype.value;applyFilter();};
const fDate=$('#fdate'); if(fDate) fDate.onchange=()=>{dwin=fDate.value;applyFilter();};
const fClear=$('#fclear'); if(fClear) fClear.onclick=()=>{region='all';stype='all';dwin='all';
  if(fRegion)fRegion.value='all'; if(fStype)fStype.value='all'; if(fDate)fDate.value='all';
  updateClear(); render();};
const qi=$('#q');
if(qi) qi.oninput=()=>{q=qi.value.trim().toLowerCase();
  if(q){ if($('#feed-list').style.display==='none'){ layer='all';
      $('#cat-head').textContent='Search results';
      $('#cat-lead').textContent='Matching items across every category, in the latest build.';
      showList(); }
    render();
  } else { showDir(); }
};
$$('.tchip').forEach(c=>c.onclick=()=>{
  const raw=c.dataset.term||''; q=raw.toLowerCase();
  $$('.tab').forEach(t=>t.classList.toggle('on',t.dataset.tab==='feed'));
  $$('.view').forEach(v=>v.classList.toggle('on',v.id==='view-feed'));
  const qq=$('#q'); if(qq) qq.value=raw;
  layer='all'; const h=$('#cat-head'),l=$('#cat-lead');
  if(h) h.textContent='Search: '+raw;
  if(l) l.textContent='Matching items across every category, in the latest build.';
  showList(); render(); window.scrollTo(0,0);
});
render();
renderFollowState();
(function(){ try{ const p=new URLSearchParams(location.search).get('topic'); if(p && TOPIC_LABELS && TOPIC_LABELS[p]) goToTopic(p); }catch(e){} })();
"""

LAYER_LABEL = {"research": "AI research & models", "clinical": "Clinical evidence & trials",
               "heor": "HEOR & HTA", "regulation": "Regulatory & authorisation",
               "access": "Reimbursement & coverage", "industry": "Industry & funding"}

# how the six layers cluster on the Feed tab
LAYER_GROUPS = [
    ("Research & evidence", ["research", "clinical"]),
    ("Commercial pathway", ["regulation", "heor", "access"]),
    ("Market activity", ["industry"]),
]

# self-explanatory name + what the feed represents (shown at the top of each list)
LAYER_NAV = {
    "research": ("AI research & models",
        "Frontier AI research, models and methods — an early signal of emerging AI capabilities that may influence future healthcare applications."),
    "clinical": ("Clinical evidence & trials",
        "Does it work in patients? Peer-reviewed studies, preprints and registered "
        "trials evaluating AI in patients."),
    "heor": ("HEOR & HTA",
        "How is value assessed? Cost-effectiveness, value assessment and health "
        "technology assessment of AI."),
    "regulation": ("Regulatory & authorisation",
        "Can it be authorised for market use? Regulatory guidance and AI-enabled device authorisations."),
    "access": ("Reimbursement & coverage",
        "Will healthcare systems pay for it? Coverage decisions, coding and the pathways "
        "that turn an authorisation into revenue."),
    "industry": ("Industry & funding",
        "The business of health AI — company announcements, partnerships, funding and product launches."),
}


def _topic_text(i):
    return (i.get("source", "") + " " + i.get("title", "") + " " + i.get("summary", "")).lower()


# Follow-topics registry — curated saved filters, grouped by the three pillars plus clinical
# areas. Every item is tagged at build time with the topics it matches (deterministic rules
# only). Slugs are permanent: they become RSS feed names and shareable ?topic= URLs.
TOPICS = [
    {"slug": "ai-clinical-studies", "pillar": "Evidence", "label": "AI clinical studies",
     "pred": lambda i: i["layer"] == "clinical"},
    {"slug": "economic-endpoint-trials", "pillar": "Evidence", "label": "Economic-endpoint trials",
     "pred": lambda i: _econ_endpoint(i)},
    {"slug": "hta-value-evidence", "pillar": "Evidence", "label": "HTA & value evidence",
     "pred": lambda i: i["layer"] == "heor"},
    {"slug": "ai-research", "pillar": "Evidence", "label": "AI research & models",
     "pred": lambda i: i["layer"] == "research"},
    {"slug": "fda-ai-authorisations", "pillar": "Authorisation", "label": "FDA AI authorisations",
     "pred": lambda i: i.get("source", "").startswith("FDA — AI device")},
    {"slug": "ema-activity", "pillar": "Authorisation", "label": "EMA activity",
     "pred": lambda i: "EMA" in i.get("source", "")},
    {"slug": "mhra-updates", "pillar": "Authorisation", "label": "MHRA updates",
     "pred": lambda i: "MHRA" in i.get("source", "")},
    {"slug": "regulatory-activity", "pillar": "Authorisation", "label": "All regulatory activity",
     "pred": lambda i: i["layer"] == "regulation"},
    {"slug": "cms-coverage", "pillar": "Coverage", "label": "CMS coverage decisions",
     "pred": lambda i: "CMS" in i.get("source", "")},
    {"slug": "nice-evaluations", "pillar": "Coverage", "label": "NICE evaluations",
     "pred": lambda i: "NICE" in i.get("source", "")},
    {"slug": "ntap-activity", "pillar": "Coverage", "label": "NTAP activity",
     "pred": lambda i: "ntap" in _topic_text(i) or "new technology add-on" in _topic_text(i)},
    {"slug": "cpt-coding", "pillar": "Coverage", "label": "CPT / coding activity",
     "pred": lambda i: "cpt" in _topic_text(i)},
    {"slug": "diga-updates", "pillar": "Coverage", "label": "DiGA updates",
     "pred": lambda i: "diga" in _topic_text(i)},
    {"slug": "reimbursement-coverage", "pillar": "Coverage", "label": "All reimbursement & coverage",
     "pred": lambda i: i["layer"] == "access"},
    {"slug": "oncology-ai", "pillar": "Clinical area", "label": "Oncology AI",
     "pred": lambda i: any(w in _topic_text(i) for w in ("oncolog", "cancer", "tumour", "tumor"))},
    {"slug": "cardiology-ai", "pillar": "Clinical area", "label": "Cardiology AI",
     "pred": lambda i: any(w in _topic_text(i) for w in ("cardio", "cardiac", "heart"))},
    {"slug": "radiology-imaging-ai", "pillar": "Clinical area", "label": "Radiology & imaging AI",
     "pred": lambda i: any(w in _topic_text(i) for w in ("radiolog", "imaging", "mri", "ct scan", "x-ray", "radiograph"))},
    {"slug": "mental-health-ai", "pillar": "Clinical area", "label": "Mental-health AI",
     "pred": lambda i: any(w in _topic_text(i) for w in ("mental health", "psychiat", "depression", "anxiety"))},
    {"slug": "digital-therapeutics", "pillar": "Clinical area", "label": "Digital therapeutics",
     "pred": lambda i: any(w in _topic_text(i) for w in ("digital therapeutic", "dtx", "diga"))},
]
TOPIC_BY_SLUG = {t["slug"]: t for t in TOPICS}
POPULAR_TOPICS = ["fda-ai-authorisations", "cms-coverage", "nice-evaluations",
                  "economic-endpoint-trials", "oncology-ai", "digital-therapeutics"]
TOPIC_PILLARS = ["Evidence", "Authorisation", "Coverage", "Clinical area"]


def tag_topics(items):
    """Attach each item's matching topic slugs (used by the feed, Follow library and RSS)."""
    for i in items:
        i["topics"] = [t["slug"] for t in TOPICS if t["pred"](i)]
    return items


def _topic_counts(items):
    c = {t["slug"]: 0 for t in TOPICS}
    for i in items:
        for s in i.get("topics", []):
            if s in c:
                c[s] += 1
    return c


_TOPIC_HELP = "Follow an evidence stream — open it in Evidence, save it in this browser, or subscribe via RSS."


def _topic_row(t, count):
    slug = t["slug"]
    zero = " tpc-zero" if count == 0 else ""
    return (f'<div class="tpc{zero}">'
            f'<button class="tpc-star" data-follow="{slug}" aria-label="Save {html.escape(t["label"])} in this browser">☆</button>'
            f'<span class="tpc-l" role="button" tabindex="0" data-topic="{slug}">{html.escape(t["label"])} '
            f'<span class="tpc-n">{count}</span></span>'
            f'<a class="tpc-rss" href="feed-{slug}.xml" title="RSS feed" target="_blank" rel="noopener">RSS</a>'
            f'</div>')


def popular_topics_html(items):
    # Home strip: a curated teaser showing only topics with activity today
    counts = _topic_counts(items)
    rows = "".join(_topic_row(TOPIC_BY_SLUG[s], counts[s]) for s in POPULAR_TOPICS
                   if s in TOPIC_BY_SLUG and counts.get(s, 0) > 0)
    if not rows:
        return ""
    return ('<div class="sec">Follow topics</div>'
            f'<div class="seccap">{_TOPIC_HELP}</div>'
            f'<div class="tpc-strip">{rows}</div>')


def topic_library_html(items):
    # Evidence library: the full tracked vocabulary — every topic with its count (incl. 0),
    # grouped by pillar. Zeros are kept on purpose; they show what the monitor covers.
    counts = _topic_counts(items)
    groups = ""
    for pillar in TOPIC_PILLARS:
        rows = "".join(_topic_row(t, counts.get(t["slug"], 0)) for t in TOPICS if t["pillar"] == pillar)
        groups += f'<div class="tpc-grp"><div class="tpc-h">{html.escape(pillar)}</div>{rows}</div>'
    return ('<div class="sec nomt">Follow topics</div>'
            f'<div class="seccap">{_TOPIC_HELP}</div>'
            '<div id="your-topics"></div>'
            f'<div class="tpc-lib">{groups}</div>')


def _rss_xml(title, desc, subset):
    from email.utils import format_datetime
    base = "https://asarmah123.github.io/ai-health-evidence-monitor/"
    now_rfc = format_datetime(datetime.now(timezone.utc))
    sep = " · "
    parts = []
    for i in subset:
        d = _pdate(i.get("date", ""))
        pub = f"<pubDate>{format_datetime(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))}</pubDate>" if d else ""
        link = safe_url(i["url"])
        dsc = html.escape(i["source"] + sep + (i.get("date") or "date unknown"))
        title_html = html.escape(i["title"])
        parts.append(
            f"<item><title>{title_html}</title>"
            f"<link>{html.escape(link)}</link>"
            f'<guid isPermaLink="true">{html.escape(link)}</guid>'
            f"<description>{dsc}</description>{pub}</item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<?xml-stylesheet type="text/xsl" href="feed.xsl"?>\n'
            '<rss version="2.0"><channel>'
            f'<title>{html.escape(title)}</title>'
            f'<link>{base}</link>'
            f'<description>{html.escape(desc)}</description>'
            '<language>en-gb</language>'
            f'<lastBuildDate>{now_rfc}</lastBuildDate>'
            + "".join(parts) + '</channel></rss>')


def write_rss(items):
    """Static RSS 2.0 of the day's highest-ranked items — same data as the page, honest dates."""
    ranked = sorted(items, key=lambda i: -rank_score(i)[0])[:40]
    (DOCS / "feed.xml").write_text(
        _rss_xml("AI in Health — Evidence Monitor",
                 "Daily market intelligence on how AI advances toward approval, reimbursement and adoption.",
                 ranked), encoding="utf-8")
    (DOCS / "feed.xsl").write_text(FEED_XSL, encoding="utf-8")


def write_topic_feeds(items):
    """One RSS feed per Follow topic (feed-<slug>.xml) — deterministic subsets of the build."""
    for t in TOPICS:
        subset = sorted((i for i in items if t["slug"] in i.get("topics", [])),
                        key=lambda i: -rank_score(i)[0])[:40]
        title = "AI in Health — " + t["label"]
        desc = t["label"] + " — " + t["pillar"] + " signals from the AI in Health monitor."
        (DOCS / ("feed-" + t["slug"] + ".xml")).write_text(_rss_xml(title, desc, subset), encoding="utf-8")
    print(f"  topic feeds written: {len(TOPICS)}")


# XSLT so the RSS renders as a friendly page in a browser, while staying a valid feed for readers.
FEED_XSL = '''<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output method="html" encoding="UTF-8" indent="yes"/>
<xsl:template match="/rss/channel">
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title><xsl:value-of select="title"/> — RSS feed</title>
<style>
:root{color-scheme:light}
body{margin:0;background:#f6f5f2;color:#1a1a1a;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:40px 22px 60px}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#9c2c2c;background:#f7ecec;border:1px solid #ecd9d9;border-radius:20px;padding:4px 12px;margin-bottom:14px}
h1{font-size:24px;margin:0 0 6px}
.desc{color:#555;margin:0 0 18px}
.note{font-size:13px;color:#5a5a5a;background:#fff;border:1px solid #e6e4df;border-radius:10px;padding:12px 15px;margin-bottom:26px}
.note a{color:#1f6feb;text-decoration:none}
.item{padding:14px 0;border-top:1px solid #e6e4df}
.item .t{display:block;font-size:16px;font-weight:600;color:#1a1a1a;text-decoration:none;line-height:1.4}
.item .t:hover{color:#1f6feb}
.item .m{font-size:12.5px;color:#8a8a8a;margin-top:4px}
.foot{margin-top:30px;font-size:12px;color:#a5a5a5}
</style></head><body><div class="wrap">
<div class="badge">RSS feed</div>
<h1><xsl:value-of select="title"/></h1>
<p class="desc"><xsl:value-of select="description"/></p>
<div class="note">This is a live RSS feed. To subscribe, copy this page's URL into a feed reader such as Feedly, Inoreader or Thunderbird. Or <a><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>return to the monitor</a>.</div>
<xsl:for-each select="item">
<div class="item">
<a class="t"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute><xsl:value-of select="title"/></a>
<div class="m"><xsl:value-of select="description"/></div>
</div>
</xsl:for-each>
<div class="foot">Updated <xsl:value-of select="lastBuildDate"/></div>
</div></body></html>
</xsl:template>
</xsl:stylesheet>
'''


def render(items, hubs, dead, built, overview="", cov_html="", trend_html="", health=None, o=None, history=None, show_coverage=True, analysis_extra=""):
    order = {t: n for n, t in enumerate(TIERS)}
    # dated items first (newest first); undated ("") sort naturally to the bottom
    items.sort(key=lambda i: i["date"], reverse=True)
    items.sort(key=lambda i: order.get(i["tier"], 9))

    counts = {k: sum(1 for i in items if i["layer"] == k) for k in LAYERS}
    prior_h = (history or [])[:-1]
    def cat_delta(k):
        base = [h["layers"][k] for h in prior_h[-7:] if isinstance(h.get("layers"), dict) and k in h["layers"]]
        if len(base) < 2:
            return ""
        d = counts.get(k, 0) - sum(base) / len(base)
        if abs(d) < 1.5:
            return ""
        a, c = ("\u25B2", "up") if d > 0 else ("\u25BC", "down")
        return f'<span class="pd {c}">{a}{abs(d):.0f}</span>'

    tier_btns = "".join(
        f'<button class="f{" on" if t == "all" else ""}" data-tier="{t}">{l}</button>'
        for t, l in [("all", "All"), ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")])

    directory_html = ""
    for gname, keys in LAYER_GROUPS:
        cards = ""
        for k in keys:
            title, desc = LAYER_NAV[k]
            cards += (f'<div class="cat" role="button" tabindex="0" data-layer="{k}" data-label="{html.escape(title)}" '
                      f'data-desc="{html.escape(desc)}" style="border-left:4px solid {STAGE_COLOR.get(k, chr(35)+"ccc")}">'
                      f'<div class="cat-t">{html.escape(title)}<span class="cat-n">{counts.get(k,0)}</span>{cat_delta(k)}</div>'
                      f'<div class="cat-d">{html.escape(desc)}</div></div>')
        directory_html += (f'<div class="catgrp"><div class="grp-h">{html.escape(gname)}</div>'
                           f'<div class="catgrid">{cards}</div></div>')

    # (reference-hub panels removed from Methodology — the placeholder section was retired)
    first_build_note = ('<div class="firstrun">First build: historical comparisons will appear after future runs.</div>'
                        if (not history or len(history) < 2) else '')
    topic_library = topic_library_html(items)
    coverage_tab = '  <div class="tab" data-tab="coverage">Coverage</div>\n' if show_coverage else ''
    coverage_view = f'<div id="view-coverage" class="view">{cov_html}</div>' if show_coverage else ''

    # fresh, stateless build-status object baked into the page each run
    undated = sum(1 for i in items if not i.get("date"))
    if health:
        contrib, exp, nfail = health["contributing"], health["expected"], len(health["failed"])
    else:
        contrib = len({i["source"] for i in items}); exp = contrib
        nfail = len({d.split(":")[0].strip() for d in dead})
    status_short = f"Built {built} · {contrib} of {exp} sources updated"
    _dead_names = sorted({d.split(":")[0].strip() for d in dead})
    status_full = f"{nfail} returned nothing · {undated} undated this run"
    if _dead_names:
        status_full += " — returned nothing: " + ", ".join(_dead_names)
    build_health = (
        '<div class="sec" style="margin-top:6px">Build health</div>'
        '<div class="bh">'
        f'<div class="bh-m"><div class="bh-v">{contrib}/{exp}</div><div class="bh-l">sources updated</div></div>'
        f'<div class="bh-m"><div class="bh-v">{nfail}</div><div class="bh-l">returned nothing</div></div>'
        f'<div class="bh-m"><div class="bh-v">{undated}</div><div class="bh-l">undated \u00b7 shown as \u201cdate unknown\u201d</div></div>'
        f'<div class="bh-m"><div class="bh-v" style="font-size:13px;font-weight:600">{built}</div><div class="bh-l">last built</div></div>'
        '</div>'
        '<div class="seccap">Each build reports coverage status: updated, quiet and undated sources.</div>'
    )

    # "most active today" strip on the feed directory — mirrors the homepage insights
    active_strip = ""
    if o:
        parts = []
        if o.get("macro"):
            m = o["macro"][0]; parts.append(f'<b>{html.escape(str(m[0]))}</b> led with {m[1]} updates')
        allb_reg = o["bodies"]["regulator"]
        allb = allb_reg + o["bodies"]["payer"]
        if allb:
            tb = max(allb, key=lambda x: x[1])
            _r = "regulator" if tb in allb_reg else "HTA/payer body"
            parts.append(f'<b>{html.escape(str(tb[0]))}</b> was the most active {_r} ({tb[1]})')
        if o.get("focus"):
            f0 = o["focus"][0]; parts.append(f'<b>{html.escape(str(f0[0]))}</b> was the top clinical area ({f0[1]})')
        if parts:
            active_strip = (f'<div class="activestrip"><span class="as-l">Snapshot</span> '
                            f'{" · ".join(parts)}</div>')

    # attach a transparent importance score + reasons + jurisdiction, so the feed can
    # sort by importance/geography and show each item WHY it ranks where it does
    for i in items:
        i["score"], i["why"] = rank_score(i)
        _c = country_of(i) or ""
        i["country"] = _c
        i["region"] = MACRO.get(_c, "") if _c else ""
        i["stype"] = source_type(i)

    # Evidence-tab filter options, from what's actually in this build
    _REG_ORDER = ["North America", "Europe", "Asia-Pacific", "Middle East & Africa"]
    _regs = [r for r in _REG_ORDER if any(i.get("region") == r for i in items)]
    _ST_ORDER = ["Regulator", "HTA / payer", "Trial registry", "Journal / evidence",
                 "Preprint / research", "Industry press", "Other"]
    _stys = [s for s in _ST_ORDER if any(i.get("stype") == s for i in items)]
    region_opts = ('<option value="all">All regions</option>'
                   + "".join(f'<option value="{html.escape(r)}">{html.escape(r)}</option>' for r in _regs))
    stype_opts = ('<option value="all">All source types</option>'
                  + "".join(f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in _stys))
    date_opts = ('<option value="all">Any date</option>'
                 '<option value="7">Last 7 days</option>'
                 '<option value="30">Last 30 days</option>'
                 '<option value="90">Last 90 days</option>')

    items_json = (json.dumps(items).replace("<", "\\u003c").replace(">", "\\u003e")
                  .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    topic_labels_json = json.dumps({t["slug"]: t["label"] for t in TOPICS})
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI in Health — Clinical, Regulatory &amp; Market Access Evidence Monitor</title>
<meta name="description" content="Daily market intelligence on how AI moves through healthcare — from clinical evidence and regulatory approval to reimbursement and market adoption. Built from primary sources.">
<meta property="og:type" content="website">
<meta property="og:title" content="AI in Health — Clinical, Regulatory &amp; Market Access Evidence Monitor">
<meta property="og:description" content="Daily market intelligence on how AI moves through healthcare — from clinical evidence and regulatory approval to reimbursement and market adoption. Built from primary sources.">
<meta property="og:url" content="https://asarmah123.github.io/ai-health-evidence-monitor/">
<meta property="og:image" content="https://asarmah123.github.io/ai-health-evidence-monitor/preview.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="AI in Health — Clinical, Regulatory &amp; Market Access Evidence Monitor">
<meta name="twitter:image" content="https://asarmah123.github.io/ai-health-evidence-monitor/preview.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/rss+xml" title="AI in Health" href="feed.xml">
<style>{CSS}</style>
</head><body><div class="wrap">
<header>
<h1>AI in Health</h1>
<div class="tagline">Track the evidence, regulatory and reimbursement signals shaping healthcare AI adoption.</div>
<div class="forwhom">Daily intelligence for market-access, HEOR, regulatory, clinical and investment teams.</div>
<div class="sub">Updated {built} · {len(items)} updates</div>
</header>

<nav class="tabs" aria-label="Sections">
  <div class="tab on" data-tab="overview">Home</div>
  <div class="tab" data-tab="feed">Evidence <span class="tabcount">({len(items)})</span></div>
  <div class="tab" data-tab="analysis">Analysis</div>
{coverage_tab}  <div class="tab" data-tab="sources">Methodology</div>
  <div class="tab" data-tab="about">About</div>
</nav>

<main>
<div id="view-overview" class="view on">{overview or '<div class="dnote">Overview populates on the next build.</div>'}</div>

<div id="view-feed" class="view">
  <div class="searchbar"><input id="q" class="search" type="search" autocomplete="off"
    placeholder="Search the latest build — title, source, regulator, country, HTA body…"></div>
  <div id="feed-dir">
    {active_strip}
    {topic_library}
    <div class="sec">Browse by stage</div>
    <div class="dnote" style="margin-bottom:16px">Browse by lifecycle stage. Counts show the current build.</div>
    {directory_html}
    <div style="margin-top:6px"><span class="seeall" data-showall="1">View all {len(items)} updates →</span></div>
  </div>
  <div id="feed-list" style="display:none">
    <div class="catback" data-back="1">← All categories</div>
    <div class="cat-head" id="cat-head"></div>
    <div class="cat-lead" id="cat-lead"></div>
    <div class="fbar">{tier_btns}<span class="spacer"></span>
      <label class="sortl">Region <select id="fregion" class="sortsel">{region_opts}</select></label>
      <label class="sortl">Type <select id="fstype" class="sortsel">{stype_opts}</select></label>
      <label class="sortl">Date <select id="fdate" class="sortsel">{date_opts}</select></label>
      <label class="sortl">Sort
        <select id="sort" class="sortsel">
          <option value="importance">Importance</option>
          <option value="newest">Newest</option>
          <option value="geography">Geography</option>
          <option value="source">Source</option>
        </select></label>
      <button class="f" id="hide">Hide read</button>
      <button class="f" id="fclear" style="display:none">Clear filters</button><span class="count" id="count"></span></div>
    <div id="feed"></div>
  </div>
</div>

{coverage_view}

<div id="view-analysis" class="view">
  <div class="dnote" style="margin-bottom:14px">Activity and trends across this build. Shows where attention is moving, not the whole market.</div>
  {first_build_note}
  {analysis_extra}
  {trend_html}
</div>

<div id="view-sources" class="view">
{build_health}
  <div class="sec">How the intelligence is built</div>
  <div class="pipeline">
    <div class="pstep"><div class="pstep-n">1</div><div class="pstep-b"><div class="pstep-t">Collect</div><div class="pstep-d">Curated primary sources across regulators, HTA bodies, journals and trial registries — chosen for relevance, not volume.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">2</div><div class="pstep-b"><div class="pstep-t">Deduplicate</div><div class="pstep-d">Merge exact duplicates by link, then collapse near-duplicate stories about the same event into one.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">3</div><div class="pstep-b"><div class="pstep-t">Classify</div><div class="pstep-d">Assign an evidence stage using transparent rules.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">4</div><div class="pstep-b"><div class="pstep-t">Rank</div><div class="pstep-d">Prioritise by explicit signals — authorisations, economic-endpoint trials, major-regulator actions, recency.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">5</div><div class="pstep-b"><div class="pstep-t">Publish</div><div class="pstep-d">Rebuilt automatically every morning as a static site.</div></div></div>
  </div>
  <div class="sec">What we monitor</div>
  <div class="seccap">~65 curated sources across the evidence-to-adoption pathway. Representative examples by type — the full list and exact queries are maintained privately.</div>
  <details class="faqi"><summary>Regulators &amp; device authorisations</summary>FDA (openFDA), EMA, MHRA, US Federal Register, PMDA, NMPA, Health Canada, Swissmedic, TGA, MFDS, SFDA</details>
  <details class="faqi"><summary>HTA &amp; payer bodies</summary>NICE, CMS, IQWiG, G-BA, HAS, CADTH, PBAC, MSAC, HIRA, AIFA, TLV, Zorginstituut, HITAP, ACE</details>
  <details class="faqi"><summary>Trials, evidence &amp; journals</summary>ClinicalTrials.gov, PubMed (E-utilities), NEJM AI, Lancet Digital Health, Nature Medicine, JAMIA, medRxiv, Value in Health, PharmacoEconomics</details>
  <details class="faqi"><summary>Research &amp; industry</summary>arXiv (cs.AI / cs.LG / cs.CL), lab &amp; standards blogs; STAT, Endpoints, Fierce, MedTech Dive, MassDevice</details>
  <div class="sec" id="trust">Trust &amp; limitations</div>
  <ul class="principles">
    <li><b>No language model.</b> Classification, ranking and dating are rule-based and reproducible.</li>
    <li><b>No invented dates.</b> Dates are read from the source; when none exists the item reads “date unknown” and is excluded from date-based figures.</li>
    <li><b>No causal claims.</b> We report what changed and how unusual it is — never why, beyond what the counts support.</li>
    <li><b>Ranking is priority, not confidence.</b> Order follows explicit additive rules; every item shows its own “Why ranked” breakdown.</li>
    <li><b>Verify the primary source.</b> An automated monitor can miss, misclassify or fail to date an item, and sources may change or retract. Nothing here is regulatory, legal, financial or medical advice.</li>
  </ul>
  <div class="sec">Privacy &amp; technical details</div>
  <div class="abt">No accounts, tracking cookies or personal-data storage — your read/unread state stays in your browser only. The site is rebuilt daily from primary APIs and feeds and served as a static site via GitHub Pages. An <a href="feed.xml">RSS feed</a> of the top-ranked items is also available. The engine is open source; the curated source list and ranking configuration are maintained privately.</div>
  <div class="sec">Coverage &amp; cadence</div>
  <div class="panels">
    <div class="panel"><div class="ph">Coverage philosophy</div><div class="psub">Healthcare AI adoption depends on clinical evidence, regulatory clearance and payment pathways. We therefore prioritise primary regulators, HTA agencies, trial registries, peer-reviewed literature and established trade press. Company press releases are excluded to keep the feed independent.</div></div>
    <div class="panel"><div class="ph">Cadence</div><div class="psub">Rebuilt once each morning. Most updates are a day or two old; device authorisations reflect the FDA’s ~30-day publishing lag.</div></div>
  </div>
  <div class="sec">Frequently asked</div>
  <div class="faq">
    <details class="faqi"><summary>How often does it update?</summary>Once each morning, rebuilt automatically. Most updates are a day or two old; FDA device authorisations reflect the agency’s ~30-day publishing lag.</details>
    <details class="faqi"><summary>Where do the items come from?</summary>~65 curated primary sources — regulator, HTA and payer feeds, trial registries, peer-reviewed journals and established trade press — via official APIs and RSS. The full list and exact queries are maintained privately.</details>
    <details class="faqi"><summary>How is each item classified?</summary>By transparent, deterministic rules based on source, terminology and lifecycle signals — no machine-learning model decides an item’s stage, region or body. Every ranking exposes its own “Why ranked” breakdown.</details>
    <details class="faqi"><summary>Why does an item show “date unknown”?</summary>Dates are read from the source. When a source exposes no usable date, the item is shown as “date unknown” rather than guessed, and it is excluded from any date-based figure.</details>
    <details class="faqi"><summary>Does it use AI to write or interpret the feed?</summary>No. No language model writes summaries, scores impact, or interprets any item. Classification, ranking, dating and every count come from transparent rules with no model, so the same inputs reproduce the same output.</details>
    <details class="faqi"><summary>A source I expected is missing — why?</summary>Coverage is deliberately curated for regulatory, clinical and reimbursement relevance, not volume, and company press releases are excluded. Suggestions are welcome via the repository.</details>
  </div>

</div>

<div id="view-about" class="view">
  <div class="sec">What this is</div>
  <div class="abt">A daily monitor tracking how AI moves through healthcare — from research and clinical validation to regulation, HTA, reimbursement and adoption.</div>
  <div class="abt">It combines public signals from regulators, HTA bodies, payer organisations, trial registries, journals and industry sources into one daily briefing — framed around two adoption questions: <b>can it be sold?</b> (authorisation) and <b>will it be paid for?</b> (coverage).</div>
  <div class="abt scope">Scope: healthcare AI evidence, regulation, reimbursement and market signals — not a general AI news feed, investment advice or predictive analytics.</div>
  <div class="sec">Who it’s for</div>
  <div class="abt">Built for market-access, HEOR, regulatory and clinical teams who need evidence, regulatory and payment signals in one place, with every item traceable to a primary source. Investors tracking healthcare AI adoption may also find it useful.</div>
  <div class="sec">How it stays credible</div>
  <div class="abt">Classification and ranking are deterministic and rule-based. Dates come from the source or are shown as “date unknown.” No causal or predictive claims are made. <span class="lnk" data-goto="sources">Full methodology →</span></div>
  <div class="sec">Contact &amp; source</div>
  <div class="abt">The monitoring engine is open source and maintained by <a href="https://github.com/asarmah123" rel="noopener">@asarmah123</a>. Corrections and source suggestions are welcome via <a href="https://github.com/asarmah123/ai-health-evidence-monitor" rel="noopener">GitHub</a>. The curated source list and ranking configuration are maintained separately.</div>
</div>
</main>

<footer class="pagefoot">
  <span class="lnk" data-goto="sources">Methodology</span><span class="fdot">&#160;·&#160;</span><details class="discmore"><summary>Build details</summary>{html.escape(status_full)}</details><span class="fdot">&#160;·&#160;</span><span class="lnk" data-goto="sources" data-anchor="trust">Disclaimer</span>
</footer>
</div>
<script>const ITEMS={items_json};const TOPIC_LABELS={topic_labels_json};{JS}</script>
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

    # cadence per source — only DAILY-tier feeds are "steady", so a zero for them is worth
    # flagging as possible breakage. Weekly/monthly feeds and standing Google-News queries
    # (and irregular regulators) are quiet by design: an empty 10-day window is normal.
    tier_of = {}
    for grp in ("rss", "gnews", "federal_register", "pubmed", "ctgov", "scrape"):
        for e in cfg.get(grp, []):
            tier_of[e["name"]] = e.get("tier", "weekly")
    tier_of["arXiv"] = "daily"
    tier_of["FDA — AI device authorisations"] = "weekly"

    # Google-News standing queries are intermittent by nature — a narrow query can be empty
    # on any given day — so they are never "steady". Only true daily publisher/API feeds are.
    gnews_names = {e["name"] for e in cfg.get("gnews", [])}
    def _steady(n):
        return tier_of.get(n) == "daily" and n not in gnews_names

    # 1. per-source counts + zero-yield flags
    print(f"sources contributing: {len(by_src)} / {len(expected)} expected")
    failed = {d.split(":")[0].strip() for d in dead}
    steady_zero = [n for n in expected if _steady(n) and by_src.get(n, 0) == 0 and n not in failed]
    quiet = [n for n in expected if not _steady(n) and by_src.get(n, 0) == 0 and n not in failed]
    if steady_zero:
        print(f"  ! DAILY sources with zero items (possible breakage): {steady_zero}")
    else:
        print("  ✓ every daily, non-failed source produced at least one item")
    if quiet:
        print(f"  · quiet weekly/monthly feeds & queries (zero is normal): {len(quiet)}")

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

    # 6. undated items — dates are never estimated, so track how many carry no date,
    #    and which sources they come from (scrape link-lists are undated by design;
    #    a normally-dated RSS feed showing up here signals a date-parsing problem).
    undated = [i["id"] for i in items if not i.get("date")]
    undated_by_src = Counter(i["source"] for i in items if not i.get("date"))
    print(f"  undated items (shown as 'date unknown'): {len(undated)}")
    if undated_by_src:
        print("    by source:", dict(undated_by_src.most_common()))
    print("============================\n")

    health = {
        "contributing": len(by_src),
        "expected": len(expected),
        "failed": sorted(failed),
        "zero_steady": sorted(steady_zero),
        "quiet": sorted(quiet),
        "undated": len(undated),
        "undated_by_src": dict(undated_by_src.most_common()),
        "by_layer": dict(by_layer),
    }
    _emit_ci_health(health, dead)
    return health


def _emit_ci_health(health, dead):
    """Surface pipeline health to GitHub Actions: inline warning annotations plus a
    job-summary table. Additive only — these prints are harmless when run locally.
    Set FAIL_ON_DEGRADE to make the job fail when >20% of steady sources go silent."""
    steady, failed = health["zero_steady"], health["failed"]
    for n in steady:
        print(f"::warning title=Feed silent::{n} produced 0 items this run (possible breakage)")
    for n in failed:
        print(f"::warning title=Feed error::{n} returned an error this run")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        ubs = health.get("undated_by_src", {})
        undated_line = ""
        if ubs:
            undated_line = " · " + ", ".join(f"{k} ({v})" for k, v in list(ubs.items())[:8])
        lines = [
            "### AI-in-Health build health", "",
            f"- Sources contributing: **{health['contributing']} / {health['expected']}**",
            f"- Silent daily feeds (possible breakage): **{len(steady)}** {', '.join(steady) or '—'}",
            f"- Errored: **{len(failed)}** {', '.join(failed) or '—'}",
            f"- Quiet (weekly/monthly feeds & queries — zero is normal): **{len(health['quiet'])}**",
            f"- Undated items: **{health['undated']}**{undated_line}", "",
        ]
        if dead:
            lines += ["<details><summary>Failure detail</summary>", ""]
            lines += [f"- `{d}`" for d in sorted(dead)]
            lines += ["", "</details>"]
        try:
            with open(summ, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    if os.environ.get("FAIL_ON_DEGRADE"):
        frac = len(steady) / (health["expected"] or 1)
        if frac > 0.20:
            print(f"::error title=Pipeline degraded::{len(steady)} steady sources "
                  f"({frac:.0%}) produced nothing — exceeds the 20% threshold")
            sys.exit(1)


def _pdate(s):
    from datetime import datetime
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# Words that carry no story identity — grammar, news/regulatory boilerplate, and very
# generic nouns. Stripped before comparing titles so only distinctive tokens remain.
_DUP_STOP = {
    "the","a","an","and","or","of","for","to","in","on","with","by","from","at","as","is",
    "are","be","was","were","that","this","it","its","their","after","over","into","amid",
    "following","up","out","new","news","update","updates","report","reports","announce",
    "announces","announced","receive","receives","received","get","gets","secure","secures",
    "launch","launches","launched","first","could","may","will","says","said","help","make",
    "using","use","used","based","via","amp","health","care","medical","device","devices",
    "system","systems","technology","tech","digital","data","company","inc","ltd","corp",
    "corporation","cleared","clearance","approval","approved","approve","recommend",
    "recommends","recommended","recommendation","license","licence","authorisation",
    "authorization","authorised","authorized","guidance","draft","final","rule","ruling",
    "us","uk","eu","platform","solution","solutions","service","services",
}


def _dup_tokens(title):
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).replace("licence", "license")
    return {w for w in t.split() if len(w) >= 3 and w not in _DUP_STOP}


def collapse_near_duplicates(items):
    """Collapse the SAME story surfaced by several outlets (e.g. via Google-News queries):
    different URLs, near-identical titles. Deterministic — clusters items that share a
    distinctive (rare) token AND enough title overlap, then keeps the most complete one.
    Exact-URL de-dup runs first; this catches what that can't."""
    if len(items) < 2:
        return items
    from collections import Counter
    toks = [_dup_tokens(i.get("title", "")) for i in items]
    df = Counter(w for s in toks for w in s)
    parent = list(range(len(items)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    # only compare items that share a rare, distinctive token (a likely entity/product name)
    by_rare = {}
    for idx, s in enumerate(toks):
        for w in s:
            if len(w) >= 4 and 2 <= df[w] <= 6:
                by_rare.setdefault(w, []).append(idx)
    checked = set()
    for idxs in by_rare.values():
        for a in range(len(idxs)):
            for bb in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[bb]
                key = (i, j) if i < j else (j, i)
                if key in checked:
                    continue
                checked.add(key)
                inter = toks[i] & toks[j]
                if not inter:
                    continue
                # the overlap must include a rare, distinctive token (an entity/product name),
                # not just generic words — this is what stops different stories from merging
                rare_entity = any(len(w) >= 4 and 2 <= df[w] <= 6 for w in inter)
                mn = min(len(toks[i]), len(toks[j])) or 1
                strong = len(inter) >= 3 or (len(inter) >= 2 and len(inter) / mn >= 0.6)
                if rare_entity and strong:
                    union(i, j)
    clusters = {}
    for idx in range(len(items)):
        clusters.setdefault(find(idx), []).append(idx)
    order = {n: n for n in range(len(items))}
    keep = []
    for members in clusters.values():
        # representative: prefer dated, then the fullest title, then the highest rank
        best = max(members, key=lambda k: (1 if items[k].get("date") else 0,
                                           len(items[k].get("title", "")),
                                           rank_score(items[k])[0]))
        keep.append((order[best], items[best]))
    dropped = len(items) - len(keep)
    if dropped:
        print(f"  near-duplicate stories collapsed: {dropped}")
    keep.sort(key=lambda t: t[0])
    return [it for _, it in keep]


def validate_or_abort(items):
    """Pre-publish QA gate. Drops items that cannot be shown or cited (missing title/
    source/layer, or a non-http(s) URL), blanks impossible future dates, and ABORTS the
    build with a non-zero exit on systemic corruption or an empty result — so CI skips the
    commit and the previous good site stays live. Deterministic; no network, no model."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    clean, dropped, fixed_dates = [], [], 0
    for i in items:
        title = (i.get("title") or "").strip()
        url = (i.get("url") or "").strip()
        src = (i.get("source") or "").strip()
        if (not title or not src or i.get("layer") not in LAYERS
                or not (url.startswith("http://") or url.startswith("https://"))):
            dropped.append(url or title or "?")
            continue
        d = _pdate(i.get("date", ""))
        if d and d > today:                 # future date = feed anomaly → mark unknown, keep item
            i["date"] = ""
            fixed_dates += 1
        clean.append(i)
    total, ndrop = len(items), len(dropped)
    print(f"  QA gate: {len(clean)} valid · {ndrop} dropped (unusable) · {fixed_dates} future dates blanked")
    if ndrop:
        print("    dropped:", dropped[:10], "…" if ndrop > 10 else "")
    if not clean:
        print("::error title=Empty build::0 valid items after QA — aborting so the previous site stays live")
        sys.exit(1)
    if total and ndrop / total > 0.25:
        print(f"::error title=Data corruption::{ndrop}/{total} items ({ndrop/total:.0%}) failed QA — aborting")
        sys.exit(1)
    return clean


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()

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

    # de-dupe by exact URL, then collapse near-duplicate stories (same event, many outlets)
    uniq = {i["id"]: i for i in items}
    items = list(uniq.values())
    items = collapse_near_duplicates(items)
    items = validate_or_abort(items)   # QA gate: drop unusable items; abort on systemic corruption
    items = tag_topics(items)   # attach Follow-topic slugs to each item
    # HEOR lens removed: it was LLM-generated, which conflicts with the site's
    # deterministic, no-model positioning. Cache retained only for the "seen" timestamp.
    cache, cache_sha = load_cache(token)
    now = datetime.now(timezone.utc)
    for i in items:
        cache[i["id"]] = {"seen": cache.get(i["id"], {}).get("seen", now.isoformat())}
    save_cache(cache, token, cache_sha)

    cov_data = load_coverage()
    draft = bool(cov_data and cov_data.get("draft"))
    agg = None if draft else coverage_aggregates(cov_data)
    sample = bool(cov_data and cov_data.get("sample"))
    if draft:
        print("  coverage: draft — panel hidden until verified")
    elif agg:
        print(f"  coverage: {agg['n_devices']} devices tracked{' (SAMPLE)' if sample else ''}")

    health = diagnostics(items, cfg, dead)

    o = overview_stats(items)
    # Editor's take removed: an LLM-written banner conflicts with the site's
    # deterministic, reproducible, no-model positioning. Home leads with rule-based
    # Key insights and the priority digest instead.
    take = ""

    row, history = log_history(items, cfg.get("trend_terms", []), token, health)
    print(f"  history: {row['total']} items logged for {row['date']} ({len(history)} builds on record)")

    home_html, analysis_extra = overview_html(items, agg, o, history, take)
    render(items, cfg["hubs"], dead, now.strftime("%d %b %Y %H:%M UTC"),
           home_html, coverage_html(agg, sample), trends_html(items, history),
           health=health, o=o, history=history, show_coverage=bool(agg),
           analysis_extra=analysis_extra)
    write_rss(items)
    write_topic_feeds(items)

    # output guard: the page and feed must be well-formed before this run is allowed to
    # publish. A non-zero exit here makes CI skip the commit, so the previous site stays.
    idx = DOCS / "index.html"
    if not idx.exists() or idx.stat().st_size < 5000:
        print("::error title=Bad output::index.html missing or too small — aborting publish")
        sys.exit(1)
    try:
        import xml.dom.minidom as _md
        _md.parseString((DOCS / "feed.xml").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"::error title=Bad feed::feed.xml is not well-formed XML ({type(e).__name__}) — aborting publish")
        sys.exit(1)

    print(f"\n✓ docs/index.html — {len(items)} items")
    if dead:
        print(f"! {len(dead)} feed(s) failed: {'; '.join(dead)}")


if __name__ == "__main__":
    main()
# build engine — end of file
