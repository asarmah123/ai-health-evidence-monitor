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


# Plain-language, honest reasons for why an item is the day's top story. We state the
# RULE that surfaced it — never an invented explanation of significance.
WHY_TEXT = {
    "Device authorisations": "Regulatory clearance is the first major step toward commercial deployment. "
                             "Clearances publish with a lag, so the decision date shown may be weeks old.",
    "Trials · economic endpoint": "An economic endpoint is the strongest early sign a product is being "
                                  "built for reimbursement, not just approval — a payer dossier forming.",
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

# What each term CATEGORY represents in the market-access framework — descriptive only.
# Explains what the category tracks, never why a specific term moved (no causal claim).
CATEGORY_MEANING = {
    "Payment / reimbursement": "the \u2018will it be paid?\u2019 side of market access",
    "Regulatory": "the \u2018can it be sold?\u2019 authorisation side",
    "HEOR / evidence": "value and cost-effectiveness evidence",
    "AI capability": "an emerging AI capability that may feed future healthcare use",
    "Evaluation / safety": "validation, bias and safety scrutiny",
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
    "BfArM": "Germany\u2019s Federal Institute for Drugs and Medical Devices",
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
    "AIFA": "Italy\u2019s Medicines Agency (Agenzia Italiana del Farmaco)",
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
              "regulation": "Regulatory approval", "heor": "Health economics",
              "access": "Coverage decision", "industry": "Market activity"}
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

    # ---- the two market-access gates, then leading indicators
    def render_tiles(rows):
        return "".join(
            f'<div class="tile"><div class="tl">{t}</div><div class="tv">{v}</div>'
            f'<div class="ts">{sub if "&" in sub else html.escape(sub)}</div></div>' for t, v, sub in rows)
    gate_tiles = [
        ("Gate 1 · Can it be sold?", len(o["clears"]),
         "Recent AI device authorisations in the FDA openFDA record (510(k)/PMA). Clearances "
         "publish with a lag, so this is roughly the last 30 days, not strictly today."),
        ("Gate 2 · Will it be paid?", len(o["coverage_actions"]),
         "Recent payment decisions from the bodies we track as feeds — CMS (US) and NICE (UK)."),
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
        if rows:
            peak = rows[0][1] or 1
            bars = "".join(
                f'<div class="trow"><div class="tn">{gloss(lbl)}</div>'
                f'<div class="tb"><div class="tf" style="width:{n/peak*100:.0f}%;background:{color}"></div></div>'
                f'<div class="tp" style="color:{color}">{n}</div></div>' for lbl, n in rows[:6])
            return f'<div class="panel"><div class="ph">{title}</div><div class="psub">{sub}</div>{bars}</div>'
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
                 f'<div class="psub">market-access activity today</div>'
                 f'<div class="subh">By region</div>{geo_rows(o.get("macro", []))}'
                 f'<div class="subh" style="margin-top:9px">{_clabel}</div>{geo_rows(o.get("countries", []))}</div>')
    regulators_panel = bar_panel("Regulators", "market-authorisation bodies (FDA, EMA)",
                                 bodies.get("regulator", []), "No regulator activity today.", color="#2f6f9f")
    payers_panel = bar_panel("HTA &amp; payer bodies", "coverage &amp; assessment (CMS, NICE)",
                             bodies.get("payer", []), "No HTA / payer activity today.", color="#1f8a70")
    clinfocus = bar_panel("Clinical focus", "therapeutic areas mentioned today",
                          o.get("focus", []), "No specialty clearly identified today.", color="#9c2c44")
    pathway = bar_panel("Reimbursement pathways in the news", "updates mentioning each route, today",
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
            ln_mover = (f'<b>{LONG[k]}</b> produced the biggest {"increase" if d > 0 else "decrease"} this week '
                        f'({"+" if d > 0 else "−"}{abs(d):.0f} vs last week)')
    # is the day's leading body unusual vs its OWN recent norm? (needs body history to accrue)
    allb = o["bodies"]["regulator"] + o["bodies"]["payer"]
    if allb:
        bname, bcnt = max(allb, key=lambda x: x[1])
        bbase = [h["bodies"].get(bname, 0) for h in prior_h[-28:] if h.get("bodies")]
        if len(bbase) >= 3:
            bavg = sum(bbase) / len(bbase)
            if bcnt - bavg >= 2:
                ln_body = (f'<b>{html.escape(bname)}</b> is above its recent norm '
                           f'({bcnt} vs a typical ~{bavg:.0f})')
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
            if tavg >= 1:
                _mult = tnow / tavg
                _mr = round(_mult)
                _lead = (f'about {_mr}\u00d7 their recent daily average'
                         if abs(_mult - _mr) <= 0.25 and _mr >= 2
                         else 'well above their recent daily average')
                _det = f'({tnow} vs ~{tavg:.0f} per build)'
            else:
                _lead = 'well above their recent daily average'
                _det = f'({tnow} vs under 1 per build)'
            ln_term = f'Mentions of <b>{html.escape(tterm)}</b> are running {_lead} {_det}'
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
        _kind_html = f'<span class="ts-kind">{_kind}</span>' if _kind else ""
        topstory = (f'<div class="topstory" data-open="{html.escape(safe_url(hi["url"]))}"><div class="topstory-l">Top story</div>'
                    f'<a class="topstory-t" href="{safe_url(hi["url"])}" target="_blank" rel="noopener">{html.escape(hi["title"])}</a>'
                    f'<div class="topstory-m">{_kind_html}<span class="ts-src">{html.escape(hi["source"])}</span>'
                    f'<span class="ts-date">{_fmt_date(hi["date"])}</span></div>'
                    f'<div class="topstory-why"><b>Why it matters:</b> {html.escape(why_text)}</div></div>')
    else:
        topstory = ('<div class="topstory quiet"><div class="topstory-l">Top story</div>'
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
    # dominant clinical areas — a real distribution insight (no fabricated cause)
    focus = o.get("focus", [])
    if focus:
        tops = " and ".join(f"<b>{html.escape(t)}</b>" for t, _ in focus[:2])
        ln_clin = f"Most-mentioned clinical areas: {tops}"
    hero_lines = [x for x in (ln_mover, ln_term, ln_body, ln_clin) if x][:3]  # region/regulator lives in Snapshot + Market breakdown
    if hero_lines:
        hl = "".join(f'<div class="hero-line">{x}</div>' for x in hero_lines)
        hero = (f'<div class="hero"><div class="hero-h">Key insights</div>{hl}</div>')
    else:
        hero = ""

    pathway_row = (f'<div class="panels" style="margin-top:8px">{pathway}{prof_panel}</div>'
                   if prof_panel else f'<div style="margin-top:8px">{pathway}</div>')
    take_html = (f'<div class="take"><div class="take-l">Editor\'s take</div>'
                 f'<div class="take-t">{html.escape(take)}</div></div>') if take else ""

    # ---- today's brief: level-1 scannable counts (all real, from this build) ----
    def _bm(v, singular, hot=False, note=""):
        cls = "brief-v hot" if (hot and v) else "brief-v"
        label = singular if v == 1 else singular + "s"   # simple singular/plural
        label = label[0].upper() + label[1:] if label else label
        note_html = f'<div class="brief-note">{note}</div>' if note else ""
        return (f'<div class="brief-m"><div class="{cls}">{v}</div>'
                f'<div class="brief-l">{label}</div>{note_html}</div>')
    _totp = [sum(h["layers"].values()) for h in prior if isinstance(h.get("layers"), dict) and h.get("layers")]
    _updnote = f"typical ~{sum(_totp)/len(_totp):.0f}" if len(_totp) >= 3 else ""
    brief = ('<div class="brief">'
             + _bm(len(items), "update", note=_updnote)
             + _bm(len(o["clears"]), "AI device<br>authorisation", hot=True)
             + _bm(len(o["coverage_actions"]), "coverage<br>decision", hot=True)
             + _bm(len(o["trials"]), "clinical<br>trial")
             + _bm(len(o["papers"]), "HEOR<br>paper")
             + '</div>')
    brief_block = ('<div class="sec" style="margin-top:6px">The brief</div>'
                   '<div class="seccap">Today\u2019s intelligence snapshot.</div>' + brief)
    cta = '<button class="cta" data-goto="feed">Browse today\u2019s updates \u2192</button>'

    _l = o["layers"]
    _top2 = [k for k, _ in sorted(_l.items(), key=lambda kv: -kv[1])[:2]]
    SUMN = {"research": "AI research", "clinical": "clinical evidence",
            "regulation": "regulatory", "heor": "health economics",
            "access": "reimbursement", "industry": "industry"}
    _areas = " and ".join(SUMN[k] for k in _top2)
    _nc = len(o["clears"])
    _auth = (f'{_nc} new AI device authorisation{"" if _nc == 1 else "s"} in this build'
             if _nc else "no new AI device authorisations in this build")
    editorial = f'<div class="editorial">Most of today\u2019s updates fall in {_areas}; {_auth}.</div>'
    return f'''{take_html}
<div class="briefing-h">Today’s briefing</div>
<div class="briefing">
{topstory}
{hero}
{brief_block}
{editorial}
{cta}
</div>
{digest}
<details class="ovsec" open><summary class="secsum">Signals to watch</summary>
<div class="seccap">Evidence forming before a product reaches either gate — an economic trial endpoint signals a payer dossier in the making. Leading indicators, not forecasts.</div>
<div class="tiles g2">{ind_html}</div></details>
<div class="sec">The evidence journey</div>
<div class="seccap">How the latest build maps to the path a product travels — from research and clinical evidence, through regulatory approval, to health-economic value, coverage and market activity. Arrows mark the change vs the past week.</div>
{journey_html}
<details class="ovsec"><summary class="secsum">The two commercial hurdles</summary>
<div class="seccap">The two hurdles most AI products must clear, in order. Each tile counts recent updates about that gate.</div>
<div class="tiles g2">{gate_html}</div></details>
{cov_mini}
<details class="more">
<summary>Market breakdown</summary>
<div class="seccap">Updates by market, regulatory and HTA body, clinical area, and reimbursement route.</div>
<div class="panels">{geo_panel}{regulators_panel}</div>
<div class="panels" style="margin-top:8px">{payers_panel}{clinfocus}</div>
{pathway_row}
</details>'''


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
        return '<div class="dnote">No history yet — the first build has just run.</div>'
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

    # top trend — lead with the absolute count, not the percentage
    tod = ""
    if movers and movers[0][0] > 0:
        pct, term, now, avg = movers[0]
        avg_txt = f"~{avg:.0f}" if avg >= 1 else "under 1"
        gap = 0
        for h in reversed(prior):
            if h.get("terms", {}).get(term, 0) > 0:
                break
            gap += 1
        newflag = f'<span class="newflag">New · first mention in {gap} builds</span>' if gap >= 3 else ""
        cls = TERM_CLASS.get(term, "")
        cls_html = f'<div class="tclass-lg">{html.escape(cls)} term</div>' if cls else ""
        meaning = CATEGORY_MEANING.get(cls, "")
        watch_html = (f'<div class="topstory-why" style="margin-top:7px"><b>What it tracks:</b> {meaning}.</div>') if meaning else ""
        tod = (f'<div class="topstory"><div class="topstory-l">Top signal today</div>'
               f'<div class="topstory-t">{html.escape(TERM_DISPLAY.get(term, term))}{newflag}</div>{cls_html}'
               f'<div class="topstory-why"><b>{now} mention{"s" if now != 1 else ""} today</b> · '
               f'typical {avg_txt} per build · (<b>{"+" if pct >= 0 else ""}{pct:.0f}%</b> vs baseline). '
               f'This tracks how often the term appears across updates in this build; it does not explain why.</div>{watch_html}</div>')

    # build volume
    spark = ""
    if len(history) >= 3:
        vals = [h["total"] for h in history[-42:]]
        hi, lo = max(vals), min(vals)
        rng = (hi - lo) or 1
        w, ht = 300, 44
        step = w / max(len(vals) - 1, 1)
        coords = [(n * step, ht - ((v - lo) / rng) * (ht - 6) - 3) for n, v in enumerate(vals)]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        lx, ly = coords[-1]
        spark = (f'<div class="spark"><div class="ph">Build volume</div>'
                 f'<div class="volnow">Today\u2019s build: <b>{vals[-1]}</b> updates</div>'
                 f'<div style="font-size:12px;color:#5f5f5f;margin:2px 0 8px">Today\u2019s volume is <b>{_vol_level(vals[-1], lo, hi, len(vals))}</b>.</div>'
                 f'<svg viewBox="0 0 {w} {ht}" preserveAspectRatio="none">'
                 f'<polyline points="{pts}" fill="none" stroke="#9c2c2c" stroke-width="1.6" '
                 f'stroke-linejoin="round" opacity=".8"/>'
                 f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" fill="#9c2c2c"/></svg>'
                 f'<div class="sparl">Range over last {len(vals)} builds: {lo}–{hi} updates per build (latest = dot).</div></div>')
    else:
        spark = ('<div class="spark"><div class="ph">Build volume</div>'
                 '<div class="psub">A sparkline appears once there are 3+ builds on record.</div></div>')

    # biggest changes in mentions
    if len(prior) >= 3 and movers:
        top = (movers[:5] + [("sep",)] + movers[-2:]) if len(movers) > 7 else movers[:6]
        peak = max((abs(r[0]) for r in movers), default=1) or 1
        bars = ""
        for r in top:
            if len(r) == 1:
                bars += '<div class="tsep"></div>'
                continue
            pct, term, now, avg = r
            cls = TERM_CLASS.get(term, "")
            cls_html = f'<span class="tclass">{html.escape(cls)}</span>' if cls else ""
            if now == 0:
                bl = f"~{avg:.0f}" if avg >= 1 else "under 1"
                bars += (f'<div class="trow"><div class="tn dim">'
                         f'<span class="tnm">{html.escape(TERM_DISPLAY.get(term, term))}</span>{cls_html}</div>'
                         f'<div class="tzero">no mentions this build (baseline {bl}/build)</div></div>')
                continue
            up = pct >= 0
            newmini = (' <span class="newmini">new</span>' if avg < 0.5
                       else ' <span class="newmini lowbase">low base</span>' if avg < 2 else "")
            bars += (f'<div class="trow"><div class="tn{"" if up else " dim"}">'
                     f'<span class="tnm">{html.escape(TERM_DISPLAY.get(term, term))}{newmini}</span>{cls_html}</div>'
                     f'<div class="tb"><div class="tf{"" if up else " down"}" style="width:{min(abs(pct) / peak * 100, 100):.0f}%"></div></div>'
                     f'<div class="tp{"" if up else " dim"}">{"+" if up else ""}{pct:.0f}%</div>'
                     f'<div class="tcount">{now} vs ~{avg:.0f}</div></div>')
        terms_html = (f'<div class="panel"><div class="ph">Largest changes in tracked terms</div>'
                      f'<div class="psub">Change vs the previous 28-day average; counts shown as today vs average. '
                      f'Small bases move sharply, so read the counts alongside the percentage.</div>{bars}</div>')
    else:
        need = max(4 - len(history), 1)
        terms_html = (f'<div class="panel"><div class="ph">Largest changes in tracked terms</div>'
                      f'<div class="psub">Accruing — term trends need a few days of history. '
                      f'~{need} more to go.</div></div>')

    orgs = active_orgs(items)
    if orgs:
        peak = orgs[0][1] or 1
        bars = "".join(
            f'<div class="trow"><div class="tn" style="width:200px">{html.escape(n)}</div>'
            f'<div class="tb"><div class="tf" style="width:{k / peak * 100:.0f}%"></div></div>'
            f'<div class="tp">{k}</div></div>' for n, k in orgs)
        orgs_html = (f'<div class="panel"><div class="ph">Organisations appearing in this build</div>'
                     f'<div class="psub">Named as trial sponsors or device applicants in this build. '
                     f'Mention frequency only — not a measure of company activity, size, or market position.</div>{bars}</div>')
    else:
        orgs_html = ('<div class="panel"><div class="ph">Organisations appearing in this build</div>'
                     '<div class="psub">No sponsors or applicants identified today.</div></div>')
    return f'{tod}<div style="margin-top:8px">{terms_html}</div><div style="margin-top:8px">{spark}</div><div style="margin-top:8px">{orgs_html}</div>'
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
.disc{font-size:11.5px;color:#7a5f14;background:#fdf8e6;border:1px solid #e0cd8a;border-left:3px solid #c9a227;border-radius:8px;padding:9px 13px;margin:0 0 16px;line-height:1.45}
.pagefoot{font-size:11.5px;color:#8a8a8a;line-height:1.6;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
.pagefoot b{color:#5f5f5f}
.pagefoot-s{margin-top:9px;color:#b0b0b0;font-variant-numeric:tabular-nums}
.discmore{display:inline;margin-left:4px}
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
.topstory-m{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px;color:var(--mute);margin-top:8px}
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
.brief{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:8px 0;margin:4px 0 10px}
.brief-m{text-align:center;padding:0 6px;border-right:1px solid #efe6e6}
.brief-m:last-child{border-right:none}
.brief-v{font-size:24px;font-weight:700;line-height:1;color:var(--ink);font-variant-numeric:tabular-nums}
.brief-v.hot{color:var(--accent)}
.brief-l{font-size:11.5px;color:#666;margin-top:7px;line-height:1.3;text-align:center}
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
.ts-kind{font-size:10px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;color:#5f5e5a;background:#f1efe8;padding:2px 7px;border-radius:4px}
.ts-src{color:#4a4a4a}.ts-date{color:var(--mute)}
.briefing-h{font-size:13px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#2f2f2f;margin:6px 0 12px}
.briefing{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}
"""

JS = """
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let tier='all', layer='all', hideRead=false, q='', sort='importance';
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
function showDir(){ const qq=$('#q'); if(qq) qq.value=''; q=''; $('#feed-dir').style.display='block'; $('#feed-list').style.display='none'; }
function showList(){ $('#feed-dir').style.display='none'; $('#feed-list').style.display='block'; window.scrollTo(0,0); }
$$('.tab').forEach(t=>t.onclick=()=>goto(t.dataset.tab));
document.addEventListener('click',e=>{
  const g=e.target.closest('[data-goto]'); if(g){e.preventDefault();goto(g.dataset.goto);
    if(g.dataset.layer){layer=g.dataset.layer;
      const h=$('#cat-head'),l=$('#cat-lead');
      if(h)h.textContent=g.dataset.label||'Updates';
      if(l)l.textContent=g.dataset.desc||'';
      showList();render();}
  }
});
document.addEventListener('click',e=>{
  const c=e.target.closest('.topstory[data-open]');
  if(c && !e.target.closest('a')){ const u=c.dataset.open; if(u && u!=='#') window.open(u,'_blank','noopener'); }
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Enter'&&e.key!==' ') return;
  const t=e.target.closest('.jstep,.cat'); if(t){e.preventDefault();t.click();}
});

// feed
function render(){
  const list=ITEMS.filter(i=>tier==='all'||i.tier===tier)
                  .filter(i=>layer==='all'||i.layer===layer)
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
      ${i.lens?`<div class="lens"><b>HEOR lens →</b> ${esc(i.lens)}</div>`:''}
      <div class="acts">
        <button data-i="${i.id}">${read.has(i.id)?'Mark unread':'Mark read'}</button>
        ${(i.why&&i.why.length)?`<details class="whyrank"><summary>Why ranked · ${i.score}</summary><ul>${i.why.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}
      </div>
    </div>`).join('') || '<div class="dnote">Nothing matches — try another filter.</div>';
  $$('.acts button').forEach(b=>b.onclick=()=>{const id=b.dataset.i;read.has(id)?read.delete(id):read.add(id);save();render();});
  $('#count').textContent=`${list.length} update${list.length===1?'':'s'} · ${read.size} read`;
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
const qi=$('#q');
if(qi) qi.oninput=()=>{q=qi.value.trim().toLowerCase();
  if(q){ if($('#feed-list').style.display==='none'){ layer='all';
      $('#cat-head').textContent='Search results';
      $('#cat-lead').textContent='Matching items across every category, in the latest build.';
      showList(); }
    render();
  } else { showDir(); }
};
render();
"""

LAYER_LABEL = {"research": "AI research & models", "clinical": "Clinical evidence & trials",
               "heor": "Health economics & HTA", "regulation": "Regulatory & authorisation",
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
    "heor": ("Health economics & HTA",
        "Is it worth paying for? Cost-effectiveness, value assessment and health "
        "technology assessment of AI."),
    "regulation": ("Regulatory & authorisation",
        "Can it reach the market? Regulatory guidance and AI-enabled device authorisations."),
    "access": ("Reimbursement & coverage",
        "Will healthcare systems pay for it? Coverage decisions, coding and the pathways "
        "that turn an authorisation into revenue."),
    "industry": ("Industry & funding",
        "The business of health AI — company activity, partnerships, funding announcements and launches."),
}


def write_rss(items):
    """Static RSS 2.0 of the day's highest-ranked items — same data as the page, honest dates."""
    from email.utils import format_datetime
    base = "https://asarmah123.github.io/ai-health-evidence-monitor/"
    ranked = sorted(items, key=lambda i: -rank_score(i)[0])[:40]
    now_rfc = format_datetime(datetime.now(timezone.utc))
    parts = []
    for i in ranked:
        d = _pdate(i.get("date", ""))
        pub = f"<pubDate>{format_datetime(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))}</pubDate>" if d else ""
        link = safe_url(i["url"])
        desc = html.escape(f'{i["source"]} \u00b7 {i.get("date") or "date unknown"}')
        parts.append(
            f"<item><title>{html.escape(i['title'])}</title>"
            f"<link>{html.escape(link)}</link>"
            f'<guid isPermaLink="true">{html.escape(link)}</guid>'
            f"<description>{desc}</description>{pub}</item>")
    rss = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<rss version="2.0"><channel>'
           '<title>AI in Health \u2014 Evidence Monitor</title>'
           f'<link>{base}</link>'
           '<description>Daily market intelligence on how AI advances toward approval, reimbursement and adoption.</description>'
           '<language>en-gb</language>'
           f'<lastBuildDate>{now_rfc}</lastBuildDate>'
           + "".join(parts) + '</channel></rss>')
    (DOCS / "feed.xml").write_text(rss, encoding="utf-8")


def render(items, hubs, dead, built, overview="", cov_html="", trend_html="", health=None, o=None, history=None, show_coverage=True):
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

    EVIDENCE_HUBS = {"OHDSI", "EHDEN", "DARWIN EU", "ISPOR — AI", "HTAi"}
    RESPONSIBLE_AI = {"CHAI", "RAISE Health"}
    def _hub(h):
        return (f'<a class="hub" href="{safe_url(h["url"])}" target="_blank" rel="noopener">'
                f'<div class="n">{html.escape(h["name"])}</div><div class="d">{html.escape(h["note"])}</div></a>')
    evidence_html = "".join(_hub(h) for h in hubs if h["name"] in EVIDENCE_HUBS)
    responsible_html = "".join(_hub(h) for h in hubs if h["name"] in RESPONSIBLE_AI)
    tracker_html = "".join(_hub(h) for h in hubs if h["name"] not in EVIDENCE_HUBS and h["name"] not in RESPONSIBLE_AI)
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
        '<div class="seccap">Each build reports its own coverage \u2014 what updated, what was quiet, and what could not be dated. Transparency by default.</div>'
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
            parts.append(f'<b>{html.escape(str(tb[0]))}</b> most active {_r} ({tb[1]})')
        if o.get("focus"):
            f0 = o["focus"][0]; parts.append(f'<b>{html.escape(str(f0[0]))}</b> top clinical area ({f0[1]})')
        if parts:
            active_strip = (f'<div class="activestrip"><span class="as-l">Snapshot</span> '
                            f'{" · ".join(parts)}</div>')

    # attach a transparent importance score + reasons + jurisdiction, so the feed can
    # sort by importance/geography and show each item WHY it ranks where it does
    for i in items:
        i["score"], i["why"] = rank_score(i)
        i["country"] = country_of(i) or ""

    items_json = (json.dumps(items).replace("<", "\\u003c").replace(">", "\\u003e")
                  .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
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
<div class="sub">Updated every morning · {len(items)} updates · {built}</div>
<div class="disc">For research and information only — automated aggregation of public sources, classified by rule-based scripts. Not regulatory, legal, financial or medical advice. Always verify against the primary source before acting.</div>
</header>

<nav class="tabs" aria-label="Sections">
  <div class="tab on" data-tab="overview">Overview</div>
  <div class="tab" data-tab="feed">Feed <span class="tabcount">({len(items)})</span></div>
{coverage_tab}  <div class="tab" data-tab="trends">Trends</div>
  <div class="tab" data-tab="sources">Sources</div>
</nav>

<main>
<div id="view-overview" class="view on">{overview or '<div class="dnote">Overview populates on the next build.</div>'}</div>

<div id="view-feed" class="view">
  <div class="searchbar"><input id="q" class="search" type="search" autocomplete="off"
    placeholder="Search the latest build — title, source, regulator, country, HTA body…"></div>
  <div id="feed-dir">
    {active_strip}
    <div class="dnote" style="margin-bottom:16px">Explore the intelligence behind today’s briefing — browse by lifecycle stage, market function, regulator or evidence type. Counts show the current build.</div>
    {directory_html}
    <div style="margin-top:6px"><span class="seeall" data-showall="1">View all {len(items)} updates →</span></div>
  </div>
  <div id="feed-list" style="display:none">
    <div class="catback" data-back="1">← All categories</div>
    <div class="cat-head" id="cat-head"></div>
    <div class="cat-lead" id="cat-lead"></div>
    <div class="fbar">{tier_btns}<span class="spacer"></span>
      <label class="sortl">Sort
        <select id="sort" class="sortsel">
          <option value="importance">Importance</option>
          <option value="newest">Newest</option>
          <option value="geography">Geography</option>
          <option value="source">Source</option>
        </select></label>
      <button class="f" id="hide">Hide read</button><span class="count" id="count"></span></div>
    <div id="feed"></div>
  </div>
</div>

{coverage_view}

<div id="view-trends" class="view">{trend_html}</div>

<div id="view-sources" class="view">
{build_health}
  <div class="sec">How the intelligence is built</div>
  <div class="pipeline">
    <div class="pstep"><div class="pstep-n">1</div><div class="pstep-b"><div class="pstep-t">Collect</div><div class="pstep-d">~65 curated sources — regulators, HTA &amp; payer bodies, journals, trial registries, industry publications — via official APIs and RSS. Chosen for regulatory, clinical, reimbursement and market relevance, not volume.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">2</div><div class="pstep-b"><div class="pstep-t">Deduplicate</div><div class="pstep-d">Canonical links merge the same story from several sources.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">3</div><div class="pstep-b"><div class="pstep-t">Classify</div><div class="pstep-d">Every item into one of six evidence stages, using transparent rules based on source type, terminology and lifecycle signals (no machine-learning model).</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">4</div><div class="pstep-b"><div class="pstep-t">Rank</div><div class="pstep-d">Explicit signals — device authorisations, economic-endpoint trials, major-regulator actions, recency. Ranking reflects editorial priority, not certainty or confidence.</div></div></div>
    <div class="parrow">↓</div>
    <div class="pstep"><div class="pstep-n">5</div><div class="pstep-b"><div class="pstep-t">Rebuild &amp; publish</div><div class="pstep-d">Rebuilt automatically every morning as a static site. Privacy-preserving: no user tracking and no personal data storage.</div></div></div>
  </div>
  <div class="sec">What we monitor</div>
  <div class="seccap">~65 curated sources across the evidence-to-adoption pathway. Representative examples by type — the full list and exact queries are maintained privately.</div>
  <div class="panels">
    <div class="panel"><div class="ph">Regulators &amp; device authorisations</div><div class="psub">FDA (openFDA), EMA, MHRA, US Federal Register, PMDA, NMPA, Health Canada, Swissmedic, TGA, MFDS, SFDA</div></div>
    <div class="panel"><div class="ph">HTA &amp; payer bodies</div><div class="psub">NICE, CMS, IQWiG, G-BA, HAS, CADTH, PBAC, MSAC, HIRA, AIFA, TLV, Zorginstituut, HITAP, ACE</div></div>
  </div>
  <div class="panels" style="margin-top:8px">
    <div class="panel"><div class="ph">Trials, evidence &amp; journals</div><div class="psub">ClinicalTrials.gov, PubMed (E-utilities), NEJM AI, Lancet Digital Health, Nature Medicine, JAMIA, medRxiv, Value in Health, PharmacoEconomics</div></div>
    <div class="panel"><div class="ph">Research &amp; industry</div><div class="psub">arXiv (cs.AI / cs.LG / cs.CL), lab &amp; standards blogs; STAT, Endpoints, Fierce, MedTech Dive, MassDevice</div></div>
  </div>
  <div class="sec">Editorial principles</div>
  <ul class="principles">
    <li><b>Never invents events or dates.</b> Dates are read from the source; when none exists the item reads “date unknown”, never a guess.</li>
    <li><b>No causal claims.</b> We report what changed and how unusual it is — never why, beyond what the counts support.</li>
    <li><b>Primary sources preferred.</b> Official machine-readable feeds and APIs where they exist; carefully scoped news queries only where none does.</li>
    <li><b>Company press releases excluded</b>, to keep the feed independent.</li>
    <li><b>Ranking is priority, not confidence.</b> Order follows explicit additive rules, and every item shows its own “Why ranked” breakdown.</li>
  </ul>
  <div class="sec">Coverage &amp; cadence</div>
  <div class="panels">
    <div class="panel"><div class="ph">Coverage philosophy</div><div class="psub">Healthcare AI adoption depends on more than technical performance — it needs clinical evidence, regulatory clearance and payment pathways. So we prioritise primary regulators, HTA agencies, trial registries, peer-reviewed literature and established trade publications. Official APIs, RSS feeds and registries where available; carefully scoped queries only where no machine-readable source exists — a deliberate editorial choice, not a technical limitation.</div></div>
    <div class="panel"><div class="ph">Cadence</div><div class="psub">Rebuilt once each morning. Most updates are a day or two old; device authorisations reflect the FDA’s ~30-day publishing lag.</div></div>
  </div>
  <div class="sec">Standards, regulators and reference networks</div>
  <div class="seccap">Reference communities, standards bodies and official trackers — the landscape these sources sit within.</div>
  <div class="grp-h">Evidence &amp; standards</div>
  <div class="hubs">{evidence_html}</div>
  <div class="grp-h" style="margin-top:16px">Responsible AI</div>
  <div class="hubs">{responsible_html}</div>
  <div class="grp-h" style="margin-top:16px">Regulatory &amp; reimbursement intelligence</div>
  <div class="hubs">{tracker_html}</div>
  <div class="foot">Sources are fetched daily from primary APIs and feeds. No accounts, tracking cookies or personal-data storage. Your read state stays locally in your browser. · <a href="feed.xml">RSS feed</a></div>
</div>
</main>

<footer class="pagefoot">
  <b>Disclaimer.</b> Automated aggregation of public sources, rule-classified — not regulatory, legal, financial or medical advice. Verify against the primary source.
  <details class="discmore"><summary>Full disclaimer</summary>It can miss, misclassify, or fail to date an item, and sources may change or retract content. Nothing here is a substitute for the primary source. Dates are read from sources and never estimated; items without a usable date are shown as “date unknown.” No causal claims are made beyond what the counts support.</details>
  <div class="pagefoot-s">{html.escape(status_short)} <details class="discmore"><summary>Build details</summary>{html.escape(status_full)}</details></div>
</footer>
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

    health = diagnostics(items, cfg, dead)

    o = overview_stats(items)
    take = weekly_take(items, o, token) if not args.no_llm else ""

    row, history = log_history(items, cfg.get("trend_terms", []), token, health)
    print(f"  history: {row['total']} items logged for {row['date']} ({len(history)} builds on record)")

    render(items, cfg["hubs"], dead, now.strftime("%d %b %Y %H:%M UTC"),
           overview_html(items, agg, o, history, take), coverage_html(agg, sample), trends_html(items, history),
           health=health, o=o, history=history, show_coverage=bool(agg))
    write_rss(items)
    print(f"\n✓ docs/index.html — {len(items)} items")
    if dead:
        print(f"! {len(dead)} feed(s) failed: {'; '.join(dead)}")


if __name__ == "__main__":
    main()
# build engine — end of file
