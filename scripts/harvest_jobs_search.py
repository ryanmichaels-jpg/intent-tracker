#!/usr/bin/env python3
"""
Jobs track via the HarvestAPI **direct** API (no Apify) — SEARCH-ONLY, cost-optimized.

Calls GET https://api.harvest-api.com/linkedin/job-search and keeps only postings whose
title contains the search phrase (default: "Data Governance"). Search-only means we pay
per search PAGE (~25 postings/request), never per-posting detail fetches — roughly
$0.001/request on the Starter pack, so a full global past-month sweep is a few dollars.

Output is a normalize-ready CSV (same JOBS_HEADER as scrape.py's jobs track) dropped in
COMP_INTEL_RAW_DIR, so it feeds ingest/normalize.py with no extra mapping. Search results
carry no company website, so the Domain column stays blank (the CRM match step matches on
Company name for these rows).

LinkedIn caps any single search at ~1,000 results (~40 pages). One worldwide query
therefore returns at most ~1,000 postings; for full global coverage pass one query per
country/region via repeated --location (or --geo-id, which overrides location).

Secret: HARVESTAPI_API_KEY (https://harvest-api.com/admin/api-keys) — from .env.

Usage:
    python3 scripts/harvest_jobs_search.py --estimate-only     # offline worst-case cost, no API calls
    python3 scripts/harvest_jobs_search.py --test              # 1 page per query, live smoke test
    python3 scripts/harvest_jobs_search.py                     # worldwide, past month (capped ~1k)
    python3 scripts/harvest_jobs_search.py \
        --location "United States" --location "United Kingdom" --location India
"""
import argparse, csv, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # shared helpers (out_dir / write_csv / JOBS_HEADER)

API_BASE = "https://api.harvest-api.com"
SEARCH_PATH = "/linkedin/job-search"
PAGE_CAP = 40             # LinkedIn stops serving a search past ~1,000 results (~25/page)
EST_PAGE_SIZE = 25        # observed LinkedIn search page size (pagination.pageSize confirms live)
EST_PRICE_PER_REQUEST = 0.001  # Starter pack: $20 -> 20k requests; bigger packs are cheaper


def api_get(params, attempts=3):
    key = os.environ.get("HARVESTAPI_API_KEY")
    if not key:
        sys.exit("ERROR: HARVESTAPI_API_KEY not set (see .env).")
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{API_BASE}{SEARCH_PATH}?{qs}"
    for i in range(attempts):
        # Custom User-Agent: Cloudflare fronts the API and bans urllib's default
        # signature with a 403 error code 1010.
        req = urllib.request.Request(url, headers={
            "X-API-Key": key,
            "User-Agent": "Mozilla/5.0 (compatible; comp-intel-hub/1.0)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            transient = e.code in (429, 500, 502, 503, 504)
            if transient and i < attempts - 1:
                print(f"[harvest] {e.code}, retrying ({i+1}/{attempts})…", file=sys.stderr)
                time.sleep(5 * (i + 1))
                continue
            sys.exit(f"ERROR: HarvestAPI returned {e.code}: {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            if i < attempts - 1:
                print(f"[harvest] url error, retrying ({i+1}/{attempts})…", file=sys.stderr)
                time.sleep(5 * (i + 1))
                continue
            sys.exit(f"ERROR: HarvestAPI url error: {e}")


def clean_url(u):
    """Strip tracking query params so the same posting dedupes across shards."""
    return (u or "").split("?")[0]


def write_dropped(dropped_rows):
    """Audit file for filter tuning. Kept OUT of the raw staging dir on purpose —
    data/raw/*.csv is ingested by normalize.py, and these rows must never reach reps."""
    d = os.path.join(scrape.REPO, "data", "audit")
    os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, f"jobs_direct_dropped_{date.today().isoformat()}.csv")
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Reason", "Job Titles", "Company", "Post URL"])
        w.writeheader()
        w.writerows(dropped_rows)
    print(f"jobs_direct: wrote {len(dropped_rows)} dropped rows -> {fn}", file=sys.stderr)


def run(search, locations, geo_ids, posted_limit, max_pages, title_filter,
        dropped_out=False, sort_by="date"):
    phrase_re = re.compile(re.escape(re.sub(r"\s+", " ", search.strip())), re.I)
    queries = ([{"geoId": g} for g in geo_ids] or
               [{"location": l} for l in locations] or
               [{}])  # no location param = worldwide (single query, capped ~1k results)

    rows, seen, requests_made, fetched, dropped_rows = [], set(), 0, 0, []
    for q in queries:
        label = q.get("geoId") or q.get("location") or "worldwide"
        page, total_pages = 1, 1
        while page <= min(total_pages, max_pages):
            resp = api_get({"search": search, "postedLimit": posted_limit,
                            "sortBy": sort_by, "page": page, **q})
            requests_made += 1
            elements = resp.get("elements") or []
            pg = resp.get("pagination") or {}
            total_pages = pg.get("totalPages") or 1
            if page == 1:
                total = pg.get("totalElements") or 0
                if total > max_pages * (pg.get("pageSize") or EST_PAGE_SIZE):
                    print(f"[{label}] {total} matches exceeds the ~{max_pages}-page cap — "
                          f"shard this query by --location/--geo-id for full coverage",
                          file=sys.stderr)
            if not elements:
                break
            for job in elements:
                fetched += 1
                title = re.sub(r"\s+", " ", (job.get("title") or "").strip())
                url = clean_url(job.get("url"))
                if title_filter and not phrase_re.search(title):
                    dropped_rows.append({"Reason": "title filter", "Job Titles": title,
                                         "Company": (job.get("company") or {}).get("name", ""),
                                         "Post URL": url})
                    continue
                keys = [k for k in (job.get("id"), url) if k]
                if not keys or any(k in seen for k in keys):
                    dropped_rows.append({"Reason": "duplicate", "Job Titles": title,
                                         "Company": (job.get("company") or {}).get("name", ""),
                                         "Post URL": url})
                    continue
                seen.update(keys)
                rows.append({"Company": (job.get("company") or {}).get("name", ""),
                             "Domain": "",  # search results carry no website
                             "Signal": search,
                             "Job Titles": title,
                             "Post URL": url})
            print(f"[{label}] page {page}/{min(total_pages, max_pages)}: "
                  f"{len(elements)} fetched, {len(rows)} kept so far", file=sys.stderr)
            page += 1
        print(f"[{label}] done: {len(rows)} kept so far ({requests_made} requests)",
              file=sys.stderr)

    dropped = fetched - len(rows)
    print(f"jobs_direct: {len(rows)} posting(s) kept | {fetched} fetched; {dropped} dropped "
          f"(title filter + dedupe) | {requests_made} requests "
          f"≈ ${requests_made * EST_PRICE_PER_REQUEST:.2f}", file=sys.stderr)
    if dropped_out and dropped_rows:
        write_dropped(dropped_rows)
    if not rows:
        return None
    return scrape.write_csv("jobs_direct", scrape.JOBS_HEADER, rows)


def estimate(locations, geo_ids, max_pages):
    n_queries = len(geo_ids) or len(locations) or 1
    worst_requests = n_queries * max_pages
    print(f"[estimate] {n_queries} quer{'y' if n_queries == 1 else 'ies'} × up to {max_pages} "
          f"pages = ≤{worst_requests} requests (~{worst_requests * EST_PAGE_SIZE} postings) "
          f"≈ ${worst_requests * EST_PRICE_PER_REQUEST:.2f} worst case")


def main():
    ap = argparse.ArgumentParser(description="Search-only LinkedIn jobs scrape via direct HarvestAPI")
    ap.add_argument("--search", default="Data Governance",
                    help='title search phrase; rows kept only if the title contains it')
    ap.add_argument("--location", action="append", default=[],
                    help="location text, repeatable (one query per location); omit for worldwide")
    ap.add_argument("--geo-id", action="append", default=[],
                    help="LinkedIn geoId, repeatable (overrides --location)")
    ap.add_argument("--posted-limit", default="month", choices=["24h", "week", "month"])
    ap.add_argument("--max-pages", type=int, default=PAGE_CAP,
                    help=f"page cap per query (default {PAGE_CAP} ≈ LinkedIn's ~1k-result limit)")
    ap.add_argument("--no-title-filter", action="store_true",
                    help="keep every fetched posting (LinkedIn search is fuzzy)")
    ap.add_argument("--dropped-out", action="store_true",
                    help="also write dropped rows + reason to data/audit/ for filter tuning")
    ap.add_argument("--sort-by", default="date", choices=["date", "relevance"],
                    help="'date' for incremental pulls; 'relevance' packs exact title matches "
                         "into the ~1k-result cap (best for one-time sweeps)")
    ap.add_argument("--test", action="store_true", help="1 page per query, live smoke test")
    ap.add_argument("--estimate-only", action="store_true",
                    help="offline worst-case cost estimate, no API calls")
    a = ap.parse_args()

    max_pages = 1 if a.test else a.max_pages
    if a.estimate_only:
        estimate(a.location, a.geo_id, max_pages)
        return
    run(a.search, a.location, a.geo_id, a.posted_limit, max_pages,
        title_filter=not a.no_title_filter, dropped_out=a.dropped_out, sort_by=a.sort_by)


if __name__ == "__main__":
    main()
