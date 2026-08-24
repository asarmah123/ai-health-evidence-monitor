#!/usr/bin/env python3
"""Post-deploy freshness check. The build publishes docs/ to GitHub Pages, but Pages deploys
asynchronously and can silently lag or fail — leaving the PUBLIC site on an older build than the one
just committed. Readers would then see stale data behind a recent-looking timestamp. This script
compares the just-built manifest (docs/build.json) with the LIVE site's build.json and reports whether
the deploy actually propagated.

Deterministic comparison lives in `is_fresh()` (unit-tested); the network fetch retries because a fresh
Pages deploy typically takes 1-3 minutes. Alert-only by design: the build is already published, so a
stale result is a signal to investigate the Pages pipeline, not a reason to unpublish.

Usage:
  python verify_deploy.py --local docs/build.json \
      --url https://asarmah123.github.io/ai-health-evidence-monitor/build.json \
      --retries 10 --wait 30
Exit code 0 = live matches local (fresh); 2 = still stale after retries; 3 = could not read a manifest.
"""
import argparse, json, sys, time, urllib.request


def is_fresh(local, live):
    """True when the live manifest represents the SAME build as the local one. Keyed on generated_at
    (the exact build instant, identical in the committed file) plus taxonomy_version as a guard. Both
    must be present and equal — a missing/renamed field is treated as NOT fresh (fail closed)."""
    if not isinstance(local, dict) or not isinstance(live, dict):
        return False
    for k in ("generated_at", "taxonomy_version"):
        lv, rv = local.get(k), live.get(k)
        if not lv or not rv or lv != rv:
            return False
    return True


def _fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "verify-deploy", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="docs/build.json")
    ap.add_argument("--url", required=True)
    ap.add_argument("--retries", type=int, default=10)
    ap.add_argument("--wait", type=int, default=30, help="seconds between retries (Pages deploy lag)")
    a = ap.parse_args()
    try:
        local = json.load(open(a.local, encoding="utf-8"))
    except Exception as e:
        print(f"::error title=Deploy check::cannot read local manifest {a.local}: {e}")
        return 3
    want = f"{local.get('generated_at')} (taxonomy {local.get('taxonomy_version')})"
    for attempt in range(1, a.retries + 1):
        try:
            live = _fetch_json(f"{a.url}?_={int(time.time())}")   # cache-buster
            if is_fresh(local, live):
                print(f"✅ Live site is fresh: {want}")
                return 0
            got = f"{live.get('generated_at')} (taxonomy {live.get('taxonomy_version')})"
            print(f"attempt {attempt}/{a.retries}: live still stale — want {want}, got {got}")
        except Exception as e:
            print(f"attempt {attempt}/{a.retries}: fetch failed ({type(e).__name__}: {e})")
        if attempt < a.retries:
            time.sleep(a.wait)
    print(f"::warning title=Deploy may be stale::live site did not reach {want} after "
          f"{a.retries} attempts (~{a.retries * a.wait}s). Check the Pages deployment.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
