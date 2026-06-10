#!/usr/bin/env python3
"""
Consolidate + dedupe + normalize competitive-intent scrapes into the unified schema,
and tag NEW vs REPEAT. CRM columns (SFDC Account, Assigned SDR, Segment / Tier) are left
blank — they are filled downstream by the CRM-connected step.

Usage:
    python3 ingest/normalize.py RAW [RAW ...] --out OUT.csv [--week 2026-W23] [--history MASTER.csv]
"""
import argparse, csv, re, sys
from collections import defaultdict

SCHEMA = ["Week","Source","Company","Domain","Person Name","Title","Email",
          "Current Company","Competitor","Post Topic / Signal","Post Type","Hand Raiser",
          "# Postings","Post URL","NEW vs REPEAT","SFDC Account","Assigned SDR","Segment / Tier",
          "Profile URL"]

# Map common source-column names -> schema fields. Extend as scraper output evolves.
ENGAGEMENT_MAP = {
    "engager company":"Company","company":"Company",
    "engager name":"Person Name","name":"Person Name",
    "title":"Title","email":"Email","current company":"Current Company",
    "competitor":"Competitor","competitor post topic":"Post Topic / Signal",
    "post topic":"Post Topic / Signal","post type":"Post Type",
    "hand raiser":"Hand Raiser","post url":"Post URL","domain":"Domain",
    "profile url":"Profile URL","engager url":"Profile URL",
    "assigned rep":"Assigned SDR","assigned sdr":"Assigned SDR",
}
JOB_MAP = {
    "company":"Company","domain":"Domain","signal":"Post Topic / Signal",
    "# postings":"# Postings","postings":"# Postings","job titles":"Title",
    "post url":"Post URL",
}

def norm_key(s): return re.sub(r"\s+"," ",s.strip().lower())

def canon_url(u):
    """Collapse LinkedIn ugcPost/activity twins to a comparable key."""
    if not u: return ""
    m = re.search(r"(ugcPost|activity)-(\d+)", u)
    return m.group(2) if m else u.strip()

def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def detect_source(headers):
    h = {norm_key(x) for x in headers}
    if "engager name" in h or "name" in h and "post url" in h: return "Engagement"
    if "# postings" in h or "postings" in h or "job titles" in h: return "Job Posting"
    return "Engagement"

def to_schema(row, source, week):
    out = {c:"" for c in SCHEMA}
    out["Week"] = week
    out["Source"] = source
    mapping = ENGAGEMENT_MAP if source == "Engagement" else JOB_MAP
    for k,v in row.items():
        field = mapping.get(norm_key(k))
        if field and (v or "").strip():
            out[field] = v.strip()
    return out

def dedupe(rows):
    seen = {}
    for r in rows:
        key = (norm_key(r["Person Name"]), norm_key(r["Company"]),
               norm_key(r["Post Topic / Signal"]), canon_url(r["Post URL"]))
        prev = seen.get(key)
        # prefer the 'activity' URL over 'ugcPost' when collapsing twins
        if prev is None or ("activity" in r["Post URL"] and "ugcPost" in prev["Post URL"]):
            seen[key] = r
    return list(seen.values())

def tag_new_repeat(rows, history_people):
    posts_by_person = defaultdict(set)
    for r in rows:
        posts_by_person[(norm_key(r["Person Name"]), norm_key(r["Company"]))].add(
            canon_url(r["Post URL"]) or norm_key(r["Post Topic / Signal"]))
    for r in rows:
        person = (norm_key(r["Person Name"]), norm_key(r["Company"]))
        repeat = person in history_people or len(posts_by_person[person]) >= 2
        r["NEW vs REPEAT"] = "REPEAT" if repeat else "NEW"
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--week", default="backfill")
    ap.add_argument("--history", help="prior Master CSV to compute REPEAT across weeks")
    a = ap.parse_args()

    history_people = set()
    if a.history:
        for r in load_rows(a.history):
            history_people.add((norm_key(r.get("Person Name","")), norm_key(r.get("Company",""))))

    rows = []
    for path in a.raw:
        raw = load_rows(path)
        if not raw: continue
        source = detect_source(raw[0].keys())
        rows += [to_schema(r, source, a.week) for r in raw]

    rows = dedupe(rows)
    rows = tag_new_repeat(rows, history_people)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA)
        w.writeheader(); w.writerows(rows)

    n=sum(r["NEW vs REPEAT"]=="NEW" for r in rows)
    print(f"wrote {len(rows)} rows -> {a.out}  (NEW={n} REPEAT={len(rows)-n})", file=sys.stderr)

if __name__ == "__main__":
    main()
