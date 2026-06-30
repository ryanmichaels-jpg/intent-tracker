"""Wide-net orchestrator: discover comp bait/question/comparison posts beyond the
curated list, mine their commenters, and run them through the same trust layer.

DISCOVER (LinkedIn + Google) -> POST-TYPE GATE -> EXTRACT comments -> ICP filter
-> CLASSIFY -> verbatim GATE -> VERIFY -> RICHNESS -> leads-widenet-<week>.csv.

Unlike the curated track, discovered sources are UNTRUSTED, so the post-type gate
HARD-DROPS non-qualifying posts (showcase/off_topic/none) before spending on comments
or the LLM. Needs both APIFY_API_TOKEN and ANTHROPIC_API_KEY.

Run (small pilot, past month):
  python3 -m intent.run_widenet --week 2026-W27 --queries 5 --max-posts 8 \
          --cap 15 --comments 20 --posted-limit month
"""
from __future__ import annotations

import argparse
import os
from collections import Counter

from .discover import displacement_queries, union_discover
from .extract import extract_for_post
from .icp import is_competitor_employee, is_icp
from .posttype import classify_post_live
from .richness import score_richness
from .run_miner import REPO, process_comment, write_leads
from .schema import Decision, Lead


def run_widenet(week: str, queries_cap: int = 5, max_posts: int = 8, cap_posts: int = 15,
                comments_per_post: int = 20, posted_limit: str = "month",
                use_google: bool = True, out_path: str | None = None) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("wide-net needs ANTHROPIC_API_KEY (post-type gate + classifier).")
    if not os.environ.get("APIFY_API_TOKEN"):
        raise SystemExit("wide-net needs APIFY_API_TOKEN (discovery + comments).")

    queries = displacement_queries()[:queries_cap] if queries_cap else displacement_queries()
    posts = union_discover(queries=queries, max_posts=max_posts, posted_limit=posted_limit,
                           use_google=use_google, cap=cap_posts)

    leads: list[Lead] = []
    posts_gated = Counter()       # post-type gate (post-level)
    comments_filtered = Counter()  # ICP / competitor filters (comment-level)
    mined = 0
    for post in posts:
        pc = classify_post_live(post)
        if not pc.qualifies:  # untrusted source -> drop non-qualifying before spending more
            posts_gated[f"{pc.post_type.value}/{pc.pave_surface.value}"] += 1
            continue
        mined += 1
        for c in extract_for_post(post, max_items=comments_per_post):
            if not c.comment_text:
                continue
            if not is_icp(c.headline or ""):   # same ICP filter as the scrape
                comments_filtered["off_icp"] += 1
                continue
            if is_competitor_employee(c.company, c.headline):  # vendor/own staff aren't buyers
                comments_filtered["competitor_employee"] += 1
                continue
            leads.append(process_comment(c, pc, live=True))

    out_path = out_path or os.path.join(REPO, "data", "out", f"leads-widenet-{week}.csv")
    out_path, records = write_leads(leads, out_path)
    counts = Counter(r["decision"] for r in records)
    print(f"[widenet] discovered={len(posts)} mined={mined} posts_gated={dict(posts_gated)} | "
          f"comments_filtered={dict(comments_filtered)} | leads={len(records)} decisions={dict(counts)}")
    print(f"[widenet] wrote {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--queries", type=int, default=5, help="cap on discovery queries (0 = all)")
    ap.add_argument("--max-posts", type=int, default=8, help="posts per query (post-search)")
    ap.add_argument("--cap", type=int, default=15, help="max posts kept after ranking")
    ap.add_argument("--comments", type=int, default=20, help="comments per kept post")
    ap.add_argument("--posted-limit", default="month", choices=["week", "month", "3months", "6months", "year", "any"])
    ap.add_argument("--no-google", action="store_true", help="LinkedIn search only")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    run_widenet(a.week, queries_cap=a.queries, max_posts=a.max_posts, cap_posts=a.cap,
                comments_per_post=a.comments, posted_limit=a.posted_limit,
                use_google=not a.no_google, out_path=a.out)


if __name__ == "__main__":
    main()
