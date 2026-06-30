"""DISCOVER (wide-net): find comp engagement-bait / question / comparison posts.

Two unioned retrievers, then client-side boolean precision:
  - LinkedIn post-search (harvestapi~linkedin-post-search): fuzzy keyword recall over
    the comp displacement queries; returns full body + engagement counts.
  - Google boolean (apify~google-search-scraper): real AND/OR/quotes the LinkedIn actor
    can't do; snippet-only hits get a full body + comment count via the post-detail actor.

Precision is enforced in code (the actor can't): AND = the body names a comp tool Pave
displaces (whole-word); NOT = an off-domain stop-list. Curated discovery (config/
targets.json) is unchanged; this is additive recall.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Optional

from .apify_client import run_sync

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DISP = os.path.join(REPO, "config", "comp_displacement_map.json")

POST_SEARCH_ACTOR = os.environ.get("APIFY_POST_SEARCH_ACTOR", "harvestapi~linkedin-post-search")
GOOGLE_ACTOR = os.environ.get("GOOGLE_SEARCH_ACTOR", "apify~google-search-scraper")
POST_DETAIL_ACTOR = os.environ.get("POST_DETAIL_ACTOR", "apimaestro~linkedin-post-detail")
MAX_POSTS_PER_QUERY = int(os.environ.get("APIFY_MAX_POSTS", "8"))


def _load_displacement() -> dict:
    try:
        with open(_DISP, "r", encoding="utf-8") as fh:
            return json.load(fh)["surfaces"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}


def displacement_queries() -> list[str]:
    return [q for s in _load_displacement().values() for q in s.get("queries", [])]


def _displaced_tools() -> dict:
    """tool keyword (lowercased) -> surface, for the AND-clause precision check."""
    out = {}
    for surface, s in _load_displacement().items():
        for tool in s.get("tool_keywords", []):
            out[tool.lower()] = surface
    return out


DISPLACED_TOOLS = _displaced_tools()


def matched_displaced_tools(text: str) -> list[str]:
    """Comp tools the post body names, whole-word (so 'compa' doesn't fire on 'company')."""
    low = (text or "").lower()
    return sorted({t for t in DISPLACED_TOOLS if re.search(rf"\b{re.escape(t)}\b", low)})


# Cues that the post body is engagement bait (author harvesting commenters).
BAIT_CUES = [
    "comment below", "comment 'bands'", 'comment "bands"', "comment the word",
    "comment and i", "i'll send", "i will send", "i'll share", "i'll dm", "dm you",
    "send you the", "drop a comment", "want the link", "link in the comments",
    "save this", "comment 'guide'", "comment “guide”", "\U0001f447",
]


def bait_score(text: str) -> int:
    low = (text or "").lower()
    return sum(cue in low for cue in BAIT_CUES)


def _comment_count(item: dict) -> int:
    return int((item.get("engagement") or {}).get("comments") or 0)


# NOT clause: clear non-comp domains that pollute keyword search.
STOP_LIST = [
    "recruiting agency", "staffing", "resume writer", "resume writing", "career coach",
    "real estate", "realtor", "crypto", "blockchain", "web3", "nft", "forex", "dropship",
    "network marketing", "mlm", "affiliate marketing", "cold email", "lead generation",
    "appointment setting", "open to work",
]
_FOLLOWER_HEADLINE = re.compile(r"^\s*[\d,]+\s+followers\s*$", re.I)


def excluded_reason(content: str, author_headline: str = "") -> str | None:
    blob = f"{content} {author_headline}".lower()
    for term in STOP_LIST:
        if re.search(rf"\b{re.escape(term)}\b", blob):
            return term
    if _FOLLOWER_HEADLINE.match(author_headline or ""):
        return "follower-count account"
    return None


def discover_live(queries: list[str], max_posts: Optional[int] = None,
                  posted_limit: str = "month") -> list[dict]:
    """LinkedIn post-search -> candidate posts (deduped by url)."""
    items = run_sync(POST_SEARCH_ACTOR, {
        "searchQueries": queries,
        "maxPosts": max_posts or MAX_POSTS_PER_QUERY,
        "sortBy": "relevance",
        "postedLimit": posted_limit,
        "scrapeComments": False,  # cheap discovery; comments pulled later for kept posts
    })
    by_url: dict[str, dict] = {}
    for it in items:
        url = (it.get("linkedinUrl") or it.get("shareLinkedinUrl") or "").split("?")[0]
        if not url or "/posts/" not in url or url in by_url:
            continue
        content = it.get("content") or ""
        author = it.get("author") or {}
        reactions = (it.get("engagement") or {}).get("reactions")
        rc = sum(r.get("count", 0) for r in reactions) if isinstance(reactions, list) else 0
        matched = matched_displaced_tools(content)
        by_url[url] = {
            "url": url, "title": (content.split("\n", 1)[0][:140] or "(no text)"),
            "content": content, "post_text": content,
            "author": author.get("name", ""), "author_headline": author.get("headline", ""),
            "comment_count": _comment_count(it), "reaction_count": rc,
            "bait_score": bait_score(content), "matched_tools": matched,
            "surfaces": sorted({DISPLACED_TOOLS[t] for t in matched}), "source": "native",
        }
    return list(by_url.values())


def _google_query_specs() -> list[tuple]:
    specs = []
    for surface, s in _load_displacement().items():
        for tool in s.get("tool_keywords", []):
            q = f'site:linkedin.com/posts "{tool}" ("comment" OR "I\'ll send" OR "free guide")'
            specs.append((q, tool, surface))
    return specs


def discover_google(limit: int = 10) -> list[dict]:
    specs = _google_query_specs()
    if not specs:
        return []
    items = run_sync(GOOGLE_ACTOR, {
        "queries": "\n".join(q for q, _, _ in specs),
        "resultsPerPage": limit, "maxPagesPerQuery": 1, "countryCode": "us",
    })
    term_to = {q: (tool, surface) for q, tool, surface in specs}
    by_url: dict[str, dict] = {}
    for item in items:
        tool, surface = term_to.get((item.get("searchQuery") or {}).get("term", ""), (None, None))
        for row in item.get("organicResults", []) or []:
            url = (row.get("url") or "").split("?")[0]
            if not url or "/posts/" not in url or url in by_url:
                continue
            snippet = f"{row.get('title', '')} {row.get('description', '')}".strip()
            by_url[url] = {
                "url": url, "title": (row.get("title") or snippet)[:140],
                "content": snippet, "post_text": snippet, "author": "", "author_headline": "",
                "comment_count": 0, "reaction_count": 0, "bait_score": bait_score(snippet),
                "matched_tools": [tool] if tool else matched_displaced_tools(snippet),
                "surfaces": [surface] if surface else [], "source": "google",
            }
    return list(by_url.values())


def _post_detail_index(items: list[dict]) -> dict:
    out = {}
    for it in items:
        post, stats = it.get("post") or {}, it.get("stats") or {}
        url = (post.get("url") or "").split("?")[0]
        if url:
            out[url] = {"text": post.get("text") or "",
                        "comments": int(stats.get("comments") or 0),
                        "reactions": int(stats.get("total_reactions") or 0)}
    return out


def enrich_posts(posts: list[dict]) -> list[dict]:
    """Give snippet-only Google hits a full body + comment count so they gate fairly."""
    urls = [p["url"] for p in posts]
    if not urls:
        return posts
    index = _post_detail_index(run_sync(POST_DETAIL_ACTOR, {"post_urls": urls}))
    for p in posts:
        d = index.get(p["url"])
        if d and d["text"]:
            tools = matched_displaced_tools(d["text"]) or p.get("matched_tools", [])
            p.update(content=d["text"], post_text=d["text"], comment_count=d["comments"],
                     reaction_count=d["reactions"], bait_score=bait_score(d["text"]),
                     matched_tools=tools,
                     surfaces=sorted({DISPLACED_TOOLS[t] for t in tools if t in DISPLACED_TOOLS})
                     or p.get("surfaces", []), enriched=True)
    return posts


def filter_candidates(posts: list[dict], require_tool: bool = True):
    """AND (names a displaced tool) + NOT (stop-list). Returns (kept, drop_reasons)."""
    kept, drops = [], Counter()
    for p in posts:
        ex = excluded_reason(p.get("content", ""), p.get("author_headline", ""))
        if ex:
            drops[f"stop:{ex}"] += 1
            continue
        if require_tool and not p.get("matched_tools"):
            drops["no_displaced_tool"] += 1
            continue
        kept.append(p)
    kept.sort(key=lambda p: (len(p.get("matched_tools", [])), p.get("bait_score", 0),
                             p.get("comment_count", 0)), reverse=True)
    return kept, drops


def _merge(native: list[dict], google: list[dict]) -> list[dict]:
    by_url = {p["url"]: p for p in google}
    for p in native:
        by_url[p["url"]] = p  # engagement-rich native record wins a collision
    return list(by_url.values())


def union_discover(queries: Optional[list[str]] = None, max_posts: Optional[int] = None,
                   posted_limit: str = "month", use_google: bool = True,
                   google_limit: int = 10, enrich_top: int = 30, cap: Optional[int] = None,
                   require_tool: bool = True) -> list[dict]:
    """Native + (optional) Google, enriched, boolean-filtered, capped."""
    queries = queries or displacement_queries()
    native = discover_live(queries, max_posts=max_posts, posted_limit=posted_limit)
    google = []
    if use_google:
        try:
            google = discover_google(limit=google_limit)
        except Exception as e:  # Google is additive; never let it break discovery
            print(f"[discover] google retriever skipped: {e}")
    combined = _merge(native, google)
    if enrich_top and google:
        g = sorted((p for p in combined if p.get("source") == "google"),
                   key=lambda p: (len(p.get("matched_tools", [])), p.get("bait_score", 0)),
                   reverse=True)
        try:
            enrich_posts(g[:enrich_top])
        except Exception as e:
            print(f"[discover] enrichment skipped: {e}")
    kept, drops = filter_candidates(combined, require_tool=require_tool)
    if cap:
        kept = kept[:cap]
    print(f"[discover] native={len(native)} google={len(google)} combined={len(combined)} "
          f"-> kept {len(kept)}; dropped {dict(drops)}")
    return kept
