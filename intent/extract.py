"""EXTRACT (wide-net): pull commenters (with comment text) from discovered posts.

Cookie-free comments actor (harvestapi~linkedin-post-comments) via async run+poll —
run-sync times out on it (transfer-brief gotcha). Maps each record onto the Commenter
trust-contract model, drops the post author, and dedupes to one lead per person
(longest comment = most signal). The raw scrape is NOT persisted here; the wide-net
leads CSV (gitignored) is the only output.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .apify_client import run_async
from .schema import Commenter

COMMENTS_ACTOR = os.environ.get("APIFY_COMMENTS_ACTOR", "harvestapi~linkedin-post-comments")
COMMENT_LIMIT = int(os.environ.get("APIFY_COMMENT_LIMIT", "20"))


def _company_from_headline(headline: Optional[str]) -> Optional[str]:
    m = re.search(r"\bat\s+([A-Za-z0-9][\w&.,'\- ]{1,40})", headline or "")
    return m.group(1).strip(" .|") if m else None


def _company(actor: dict) -> Optional[str]:
    cp = actor.get("currentPosition")
    if isinstance(cp, list) and cp:
        c = cp[0].get("companyName") or cp[0].get("company")
        if c:
            return c
    exp = actor.get("experience")
    if isinstance(exp, list) and exp:
        c = exp[0].get("companyName")
        if c:
            return c
    return _company_from_headline(actor.get("headline") or actor.get("position"))


def _is_author(raw: dict) -> bool:
    actor = raw.get("actor")
    return isinstance(actor, dict) and bool(actor.get("author"))


def _map_comment(raw: dict, post_url: Optional[str], competitor: Optional[str]) -> Optional[Commenter]:
    text = raw.get("commentary") or raw.get("commentText") or raw.get("text") or raw.get("comment")
    if not text or not str(text).strip():
        return None
    actor = raw.get("actor") or raw.get("author")
    if isinstance(actor, dict):
        name = (actor.get("name")
                or " ".join(p for p in (actor.get("firstName"), actor.get("lastName")) if p)
                or actor.get("fullName") or "unknown")
        headline = actor.get("headline") or actor.get("position") or actor.get("occupation")
        profile_url = actor.get("linkedinUrl") or actor.get("profileUrl") or actor.get("url")
        company = _company(actor)
    else:
        name = actor or raw.get("name") or "unknown"
        headline = raw.get("headline") or raw.get("occupation")
        profile_url = raw.get("profileUrl") or raw.get("url")
        company = _company_from_headline(headline)
    return Commenter(
        name=name, comment_text=str(text).strip(), headline=headline, company=company,
        profile_url=profile_url, competitor=competitor,
        post_url=raw.get("postUrl") or post_url, source="wide-net",
    )


def map_raw_items(raw_items: list[dict], post_url: Optional[str], competitor: Optional[str]) -> list[Commenter]:
    mapped: list[Commenter] = []
    for raw in raw_items:
        if _is_author(raw):
            continue
        c = _map_comment(raw, post_url, competitor)
        if c:
            mapped.append(c)
        for reply in raw.get("replies", []) or []:
            if _is_author(reply):
                continue
            r = _map_comment(reply, raw.get("postUrl") or post_url, competitor)
            if r:
                mapped.append(r)
    best: dict[str, Commenter] = {}
    no_url: list[Commenter] = []
    for c in mapped:
        if not c.profile_url:
            no_url.append(c)
            continue
        prev = best.get(c.profile_url)
        if prev is None or len(c.comment_text) > len(prev.comment_text):
            best[c.profile_url] = c
    return list(best.values()) + no_url


def extract_for_post(post: dict, max_items: Optional[int] = None) -> list[Commenter]:
    """Commenters on ONE discovered post. competitor = the comp tool(s) it names."""
    competitor = ", ".join(post.get("matched_tools", [])) or post.get("source", "wide-net")
    raw = run_async(COMMENTS_ACTOR, {
        "posts": [post["url"]],
        "maxItems": max_items or COMMENT_LIMIT,
        "scrapeReplies": True,
        "profileScraperMode": "main",  # need headline + profileUrl for the ICP filter
    })
    return map_raw_items(raw, post["url"], competitor)
