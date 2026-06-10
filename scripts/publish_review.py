#!/usr/bin/env python3
"""
Publish the human-review candidates to a 'Review' tab in Comp_Intel_Ready via the service
account, so they surface in the same sheet you work in each week (a headless run otherwise
just rewrites local JSON you'd never see).

Sources (gitignored, written by the scrape):
  config/watchlist_candidates.json  — bait-post authors
  config/review_candidates.json     — non-target posts

This is sheet structure only — it creates/refreshes the 'Review' tab and NEVER touches Master
or the rep tabs. To act: review the tab, then approve good authors into config/watchlist.json
(they feed next week's engagement scrape).
"""
import os, json, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # for REPO

REVIEW_TAB = "Review"
HEADER = ["Type", "Name / Company", "Profile / Author URL", "Post URL",
          "Why flagged", "Sample", "Status"]


def load(name):
    path = os.path.join(scrape.REPO, "config", name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def build_rows():
    rows = []
    for c in load("watchlist_candidates.json").get("candidates", []):
        rows.append(["Bait author", c.get("author", ""), c.get("url", ""), c.get("latest_post", ""),
                     f"{c.get('num_bait_posts', 0)} bait post(s), max engagement {c.get('max_engagement', 0)}",
                     c.get("sample", ""), c.get("status", "candidate")])
    for p in load("review_candidates.json").get("posts", []):
        rows.append(["Non-target post", p.get("author", ""), p.get("url", ""), p.get("post_url", ""),
                     f"{p.get('target_terms_hit', 0)} target term(s) — not a target author",
                     p.get("sample", ""), p.get("status", "review")])
    return rows


def main():
    sid = os.environ.get("COMP_INTEL_SHEET_ID")
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not (sid and sa and os.path.exists(sa)):
        sys.exit("ERROR: COMP_INTEL_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_FILE not configured.")
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    ss = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    titles = {s["properties"]["title"]
              for s in ss.get(spreadsheetId=sid, fields="sheets.properties.title").execute()["sheets"]}
    if REVIEW_TAB not in titles:
        ss.batchUpdate(spreadsheetId=sid,
                       body={"requests": [{"addSheet": {"properties": {"title": REVIEW_TAB}}}]}).execute()
    rows = build_rows()
    ss.values().clear(spreadsheetId=sid, range=REVIEW_TAB).execute()
    ss.values().update(spreadsheetId=sid, range=f"{REVIEW_TAB}!A1", valueInputOption="RAW",
                       body={"values": [HEADER] + rows}).execute()
    print(f"review: published {len(rows)} candidate(s) to the '{REVIEW_TAB}' tab", file=sys.stderr)


if __name__ == "__main__":
    main()
