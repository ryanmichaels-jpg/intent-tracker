#!/usr/bin/env python3
"""
Track 3 — engagement-bait discovery + hand-raiser surfacing (two-pass, cost-optimized).

PASS 1 (cheap): search target-keyword posts via /linkedin/post-search (text only, no comment
  scraping), detect bait on the post text (CTA + deliverable; hiring excluded).
PASS 2 (targeted): fetch comments ONLY on the handful of bait posts via /linkedin/post-comments.

  - SURFACE NOW: commenters on bait posts -> engagement hand-raiser rows (Source=Engagement,
    Hand Raiser=Y) in data/raw/bait_engagement_<date>.csv (enriched + ICP-gated). Reach reps
    this week; not held behind a threshold.
  - GROW THE WATCHLIST: bait-post authors -> config/watchlist_candidates.json (human-review
    gate). Approve real target creators into config/watchlist.json.

Reuses scrape.py's HarvestAPI helpers. Config: config/targets.json -> bait_discovery.
Secret: HARVEST_API_KEY.

Usage:
    python3 scripts/bait_discovery.py            # full run
    python3 scripts/bait_discovery.py --test     # small caps, live smoke test
"""
import argparse, json, os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # shared helpers (HarvestAPI transport + ICP/bait/enrichment logic)

REPO = scrape.REPO


def load_cfg():
    path = os.path.join(REPO, "config", "targets.json")
    if not os.path.exists(path):
        sys.exit("ERROR: config/targets.json not found.")
    with open(path) as f:
        return json.load(f).get("bait_discovery", {})


def load_json(name, default):
    path = os.path.join(REPO, "config", name)
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def engagement_of(post):
    e = post.get("engagement") or {}
    return sum(int(e.get(k) or 0) for k in ("likes", "comments", "shares"))


def recency_enum(days):
    return "24h" if days <= 1 else "week" if days <= 7 else "month"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="small caps, live smoke test")
    a = ap.parse_args()

    cfg = load_cfg()
    terms = cfg.get("target_terms", [])
    min_eng = cfg.get("min_engagement", 5)
    min_bait = cfg.get("min_bait_posts", 1)
    max_posts = 8 if a.test else cfg.get("max_posts_per_query", 20)
    max_comments = 10 if a.test else cfg.get("max_comments", 20)
    posted = recency_enum(cfg.get("recency_days", 7))
    if not terms:
        sys.exit("ERROR: bait_discovery.target_terms is empty.")

    approved = {scrape.norm(x.get("url", "")) for x in load_json("watchlist.json", {}).get("approved", [])}
    topic_cache = {}
    authors = {}        # candidate authors
    bait_posts = {}     # norm(post_url) -> meta ; only bait posts (text-only pass)

    # PASS 1 — text-only search via /linkedin/post-search (no comment scraping)
    for term in terms:
        items = scrape.harvest_paginate(scrape.EP_POST_SEARCH,
                                        {"search": term, "postedLimit": posted}, max_items=max_posts)
        for it in items:
            text = it.get("content") or ""
            if not (scrape.is_bait(text) and not scrape.HIRING_RE.search(text)):
                continue
            au = it.get("author") or {}
            url = it.get("linkedinUrl") or ""
            k = scrape.norm(url)
            if not k:
                continue
            eng = engagement_of(it)
            if k not in bait_posts:
                bait_posts[k] = {"text": text, "author": au.get("name") or "", "url": url,
                                 "aurl": (au.get("linkedinUrl") or "").split("?")[0],
                                 "topic": scrape.summarize_topic(text, topic_cache), "eng": eng}
            # author candidate (needs engagement threshold; not already approved)
            aurl = bait_posts[k]["aurl"]
            if au.get("name") and eng >= min_eng and scrape.norm(aurl) not in approved:
                key = scrape.norm(aurl) or scrape.norm(au["name"])
                rec = authors.setdefault(key, {"author": au.get("name"), "url": aurl,
                                               "num_bait_posts": 0, "latest_post": url,
                                               "max_engagement": 0, "sample": text[:160],
                                               "status": "candidate"})
                rec["num_bait_posts"] += 1
                rec["max_engagement"] = max(rec["max_engagement"], eng)

    # PASS 2 — comments only on the (few) bait posts, one /linkedin/post-comments query per post
    # (paginated to the cap). We fetch per post, so each comment's parent post is known directly.
    rows = {}
    for meta in bait_posts.values():
        for c in scrape.harvest_paginate(scrape.EP_POST_COMMENTS,
                                         {"post": meta["url"]}, max_items=max_comments):
            ac = c.get("actor") or {}
            name = ac.get("name") or ""
            headline = ac.get("position") or ac.get("headline") or ""
            if not name or scrape.is_non_icp(headline):
                continue
            rows[(scrape.norm(name), scrape.norm(headline)[:40], scrape.norm(meta["url"]))] = {
                "Engager Name": name, "Engager Company": scrape.parse_company(headline),
                "Title": headline, "Email": "", "Current Company": "",
                "Competitor": (meta["author"] + " (bait)") if meta["author"] else "(bait)",
                "Competitor Post Topic": meta["topic"], "Post Type": "comment",
                "Hand Raiser": "Y", "Post URL": meta["url"], "Domain": "",
                "_url": (ac.get("linkedinUrl") or "").split("?")[0]}

    out_rows = list(rows.values())
    if out_rows:
        scrape.enrich_current_company(out_rows)
        out_rows = [r for r in out_rows if scrape.is_icp(r["Title"])]  # positive Tier-1/2 gate
        for r in out_rows:
            r["Profile URL"] = r.pop("_url", "")   # persist engager profile URL for re-enrichment
        scrape.write_csv("bait_engagement", scrape.ENGAGEMENT_HEADER, out_rows)
    else:
        print("bait: no hand-raiser commenters surfaced this run", file=sys.stderr)

    candidates = [r for r in authors.values() if r["num_bait_posts"] >= min_bait]
    existing = {scrape.norm(c.get("url", "")) or scrape.norm(c.get("author", "")): c
                for c in load_json("watchlist_candidates.json", {}).get("candidates", [])}
    for c in candidates:
        k = scrape.norm(c["url"]) or scrape.norm(c["author"])
        if k in existing:
            c["num_bait_posts"] = max(c["num_bait_posts"], existing[k].get("num_bait_posts", 0))
    path = os.path.join(REPO, "config", "watchlist_candidates.json")
    with open(path, "w") as f:
        json.dump({"generated": date.today().isoformat(), "candidates": candidates}, f,
                  indent=2, ensure_ascii=False)
    print(f"bait: {len(bait_posts)} bait post(s) found; surfaced {len(out_rows)} hand-raiser(s); "
          f"{len(candidates)} author candidate(s). Approve real creators into config/watchlist.json.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
