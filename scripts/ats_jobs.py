#!/usr/bin/env python3
"""
Jobs track via Ashby + Lever public job-board APIs — FREE, no auth, no scraping vendor.

Both ATSes expose a public per-company feed (no global search — you watch a list of
company board slugs):

  Ashby: GET https://api.ashbyhq.com/posting-api/job-board/<slug>   -> {"jobs":[...]}
  Lever: GET https://api.lever.co/v0/postings/<slug>?mode=json      -> [...]

Postings are filtered the same way as harvest_jobs_search.py (title must contain the
search phrase, default "Data Governance") plus a posted-date window, and written as a
normalize-ready CSV (same JOBS_HEADER). Domain stays blank (feeds carry no website);
the CRM match step matches on Company name.

Boards to watch: config/ats_boards.json (gitignored; copy from
config/ats_boards.example.json). Slug = the last path segment of the public board URL,
e.g. https://jobs.ashbyhq.com/ramp -> ashby slug "ramp";
     https://jobs.lever.co/palantir -> lever slug "palantir".

Usage:
    python3 scripts/ats_jobs.py                          # boards from config, past month
    python3 scripts/ats_jobs.py --ashby ramp --lever palantir   # ad-hoc boards
    python3 scripts/ats_jobs.py --posted-limit week --dropped-out
"""
import argparse, csv, json, os, re, sys, time, urllib.error, urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # shared helpers (write_csv / JOBS_HEADER / REPO)

ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
WINDOWS = {"24h": timedelta(days=1), "week": timedelta(days=7),
           "month": timedelta(days=30), "all": None}


def http_json(url, attempts=3):
    for i in range(attempts):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; comp-intel-hub/1.0)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # unknown/renamed board slug
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(5 * (i + 1))
                continue
            print(f"[ats] {url} -> {e.code}, skipping", file=sys.stderr)
            return None
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
                continue
            print(f"[ats] {url} -> {e}, skipping", file=sys.stderr)
            return None


def load_boards():
    path = os.path.join(scrape.REPO, "config", "ats_boards.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def ashby_postings(slug):
    """Yield (title, url, posted_at) for a board. posted_at may be None."""
    data = http_json(ASHBY_URL.format(slug=slug))
    for job in (data or {}).get("jobs", []):
        if job.get("isListed") is False:
            continue
        ts = None
        raw = job.get("publishedAt")
        if raw:
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        yield job.get("title") or "", job.get("jobUrl") or "", ts


def lever_postings(slug):
    data = http_json(LEVER_URL.format(slug=slug))
    if data is None or isinstance(data, dict):  # dict = {"ok": false, ...}
        return
    for job in data:
        ts = None
        if job.get("createdAt"):
            ts = datetime.fromtimestamp(job["createdAt"] / 1000, tz=timezone.utc)
        yield job.get("text") or "", job.get("hostedUrl") or "", ts


def run(search, boards, posted_limit, title_filter, dropped_out):
    phrase_re = re.compile(re.escape(re.sub(r"\s+", " ", search.strip())), re.I)
    window = WINDOWS[posted_limit]
    cutoff = datetime.now(timezone.utc) - window if window else None

    rows, dropped_rows, seen, fetched = [], [], set(), 0
    fetchers = [("ashby", ashby_postings), ("lever", lever_postings)]
    for ats, fetch in fetchers:
        for slug, company in (boards.get(ats) or {}).items():
            company = company or slug.replace("-", " ").title()
            n_before = len(rows)
            for title, url, posted_at in fetch(slug):
                fetched += 1
                title = re.sub(r"\s+", " ", title.strip())
                drop = None
                if title_filter and not phrase_re.search(title):
                    drop = "title filter"
                elif cutoff and posted_at and posted_at < cutoff:
                    drop = "posted before window"
                elif not url or url in seen:
                    drop = "duplicate"
                if drop:
                    dropped_rows.append({"Reason": drop, "Job Titles": title,
                                         "Company": company, "Post URL": url})
                    continue
                seen.add(url)
                rows.append({"Company": company, "Domain": "", "Signal": search,
                             "Job Titles": title, "Post URL": url})
            print(f"[{ats}/{slug}] {len(rows) - n_before} kept", file=sys.stderr)

    print(f"jobs_ats: {len(rows)} posting(s) kept | {fetched} fetched; "
          f"{len(dropped_rows)} dropped | cost $0 (public APIs)", file=sys.stderr)
    if dropped_out and dropped_rows:
        d = os.path.join(scrape.REPO, "data", "audit")
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, f"jobs_ats_dropped_{date.today().isoformat()}.csv")
        with open(fn, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["Reason", "Job Titles", "Company", "Post URL"])
            w.writeheader()
            w.writerows(dropped_rows)
        print(f"jobs_ats: wrote {len(dropped_rows)} dropped rows -> {fn}", file=sys.stderr)
    if not rows:
        return None
    return scrape.write_csv("jobs_ats", scrape.JOBS_HEADER, rows)


def main():
    ap = argparse.ArgumentParser(description="Ashby + Lever job-board scrape (free public APIs)")
    ap.add_argument("--search", default="Data Governance",
                    help="title phrase; rows kept only if the title contains it")
    ap.add_argument("--ashby", action="append", default=[],
                    help="Ashby board slug, repeatable (adds to config boards)")
    ap.add_argument("--lever", action="append", default=[],
                    help="Lever board slug, repeatable (adds to config boards)")
    ap.add_argument("--posted-limit", default="month", choices=sorted(WINDOWS),
                    help="posted-date window (default month); 'all' = no date filter")
    ap.add_argument("--no-title-filter", action="store_true")
    ap.add_argument("--dropped-out", action="store_true",
                    help="also write dropped rows + reason to data/audit/")
    a = ap.parse_args()

    boards = load_boards()
    for slug in a.ashby:
        boards.setdefault("ashby", {}).setdefault(slug, "")
    for slug in a.lever:
        boards.setdefault("lever", {}).setdefault(slug, "")
    if not (boards.get("ashby") or boards.get("lever")):
        sys.exit("ERROR: no boards to watch. Copy config/ats_boards.example.json to "
                 "config/ats_boards.json, or pass --ashby/--lever slugs.")
    run(a.search, boards, a.posted_limit,
        title_filter=not a.no_title_filter, dropped_out=a.dropped_out)


if __name__ == "__main__":
    main()
