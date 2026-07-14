#!/usr/bin/env python3
"""
Weekly competitive-intent scrape (local machine). Two tracks on HarvestAPI's DIRECT REST API
(https://api.harvest-api.com — synchronous, paginated; no Apify actor runs/datasets):

  engagement -> /linkedin/company-posts + /linkedin/profile-posts  (posts per target),
                then /linkedin/post-reactions + /linkedin/post-comments for the top-N posts
                (reactors + commenters on competitor posts)
  jobs       -> /linkedin/job-search                               (target-role hiring signals)

The engagement track used to be one bundled actor run (posts + reactions + comments together);
the direct API splits them, so we fetch posts per target, rank them locally by engagement, and
pull reactions/comments ONLY for the selected top-N posts — cheaper than scraping everything.

Output: normalize-ready CSVs dropped into the staging folder (COMP_INTEL_RAW_DIR, normally
the local path of the Drive-for-Desktop 'comp-intel-raw' folder). The column
headers are chosen to match ingest/normalize.py's ENGAGEMENT_MAP / JOB_MAP, so the files feed
straight into the engine with no further mapping. The CSV schema is unchanged from the Apify era.

This script NEVER touches the CRM and NEVER writes to the sheet — it only produces raw files.
It does NOT change the Master schema — it just populates the existing columns better and
filters rows.

Engagement filters/derivations: drop hiring posts, competitor/own employees, company pages,
and obvious non-ICP titles; keep only target-authored posts (non-target posts go to a
review list, or surface if clearly on-target). Title + Company come from profile enrichment;
Post Topic is a short themed label (Haiku fills the misses); Hand Raiser = comment on a
bait post; engagers on a target exec's posts get that exec's Competitor label.

Config: config/targets.json (gitignored; copy from config/targets.example.json).
Secrets: HARVEST_API_KEY (required), ANTHROPIC_API_KEY (optional, topic summarizer) — from .env.

Usage:
    python3 scripts/scrape.py --estimate-only          # offline cost estimate, no API calls
    python3 scripts/scrape.py --test                   # small caps, live, smoke test
    python3 scripts/scrape.py                           # full run, both tracks
    python3 scripts/scrape.py --track jobs              # one track only
"""
import argparse, csv, json, os, re, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- HarvestAPI direct REST API (https://docs.harvestapi.io) -----------------------------
# Synchronous GET, auth via X-API-Key header, standard envelope {elements, pagination, ...}.
HARVEST_BASE = "https://api.harvest-api.com"
EP_COMPANY_POSTS = "/linkedin/company-posts"     # ?company=<url>  (or companyUniversalName)
EP_PROFILE_POSTS = "/linkedin/profile-posts"     # ?profile=<url>  (exec / personal pages)
EP_POST_REACTIONS = "/linkedin/post-reactions"   # ?post=<postUrl>
EP_POST_COMMENTS = "/linkedin/post-comments"     # ?post=<postUrl>
EP_JOB_SEARCH = "/linkedin/job-search"           # ?search=<title>&location=<loc>
EP_PROFILE = "/linkedin/profile"                 # ?url=<profileUrl>  (single, no batching)
EP_POST_SEARCH = "/linkedin/post-search"         # ?search=<term>  (used by bait_discovery)

# Per-endpoint request counter — the split API makes call volume the cost driver, so we log it.
CALL_COUNTS = {}

# HarvestAPI is pay-as-you-go: you buy credits and each returned result consumes credits (no
# per-run platform fee, unlike Apify's old 20% margin). The public list price is $4 / 1,000
# profiles ($0.004 each); posts/reactions/comments/jobs are billed per returned result at a
# similar rate. These are DEFAULTS for the offline --estimate-only projection only — confirm
# the exact per-result credit cost for your plan at harvestapi.io/pricing and override via a
# "pricing" block in config/targets.json (USD per returned result, keyed by result type).
DEFAULT_PRICING = {"post": 0.004, "reaction": 0.004, "comment": 0.004,
                   "job": 0.004, "profile": 0.004}


def _load_dotenv():
    """Load KEY=VALUE lines from the repo .env into os.environ (only keys not already set), so
    running `python3 scripts/scrape.py` standalone picks up HARVEST_API_KEY / ANTHROPIC_API_KEY
    without sourcing .env first. The scheduled runner still sources .env itself; this is a
    convenience for manual runs. Minimal stdlib parser — ignores blanks/comments; strips quotes."""
    path = os.path.join(REPO, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()

# Headers must match ingest/normalize.py maps so the drop is normalize-ready.
ENGAGEMENT_HEADER = ["Engager Name", "Engager Company", "Title", "Email", "Current Company",
                     "Competitor", "Competitor Post Topic", "Post Type", "Hand Raiser",
                     "Post URL", "Domain", "Profile URL"]
JOBS_HEADER = ["Company", "Domain", "Signal", "Job Titles", "Post URL"]  # one row per posting

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # topic summarizer fallback (cheap)

# ICP personas — neutral PLACEHOLDER defaults so the engine runs out of the box. This repo is
# ICP-agnostic: retarget it to YOUR market without editing code by adding an optional "icp" block
# to config/targets.json overriding any of these keys:
#   tier1 / tier2 / exclude / hiring  -> regex strings (matched case-insensitively)
#   themes                            -> list of [regex, label] pairs (first match wins)
# Anything omitted falls back to the default. See config/targets.example.json + docs/ADAPTING.md.
DEFAULT_ICP = {
    # Tier 1 = your core buyer persona — the most senior decision-makers for your product.
    "tier1": (r"(\bchief\b|chief\s*\w+\s*officer|\bc[a-z]o\b|\bvp\b|vice\s*president|"
              r"\bhead\s*of\b|\bdirector\b)"),
    # Tier 2 = adjacent / influencer personas worth keeping.
    "tier2": (r"(\bmanager\b|\blead\b|\bsenior\b|\bprincipal\b|\bspecialist\b)"),
    # Obvious non-buyers to drop even if a tier word slips into the headline.
    "exclude": (r"\b(software\s*engineer|developer|data\s*scien\w*|data\s*entry|machine\s*learning|"
                r"designer|engineer|teacher|student|\bintern\b|recruiter|sourcer|"
                r"sales\s*development|account\s*executive|inside\s*sales|"
                r"sales\s*(executive|rep|representative|manager|lead|leader|director|operations)|"
                r"\bsdr\b|\bbdr\b|investor|venture|attorney|legal|professor|instructor|adjunct|"
                r"lecturer|physical\s*therapist|\bnurse\b|customer\s*success|coach)\b"),
    # Post Topic: deterministic theme map first (short label); LLM only fills the misses.
    # These are neutral examples — replace with themes that matter for YOUR market.
    "themes": [
        [r"pricing|cost|budget|spend", "Pricing & Budget"],
        [r"headcount|hiring\s*plan|org\s*design|workforce\s*plan", "Workforce Planning"],
        [r"tooling|platform|software|stack|migration", "Tooling & Platforms"],
        [r"process|workflow|automation|efficiency", "Process & Automation"],
    ],
    # Hiring / job-opening posts are low signal — skip the whole post (and its engagers).
    "hiring": (r"(\bwe'?re hiring\b|\bnow hiring\b|\bwe are hiring\b|\bhiring an?\b|\bjob opening\b|"
               r"\bopen role[s]?\b|\bopen position[s]?\b|\bjoin our team\b|\bjoin the team\b|\bapply now\b|"
               r"\bwe'?re looking to hire\b|#hiring|\bnow recruiting\b|\bwe'?re growing\b)"),
}


def _load_icp_overrides():
    """Soft-load the optional 'icp' block from config/targets.json (missing file or block -> {}),
    so importing this module never hard-fails on a fresh clone before targets.json exists."""
    path = os.path.join(REPO, "config", "targets.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f).get("icp", {}) or {}
    except (ValueError, OSError):
        return {}


# Config overrides win over defaults; an omitted/empty key keeps the default. An engager is KEPT
# only if their (enriched) title matches Tier 1 or Tier 2 and is not on the EXCLUDE list.
_ICP = {**DEFAULT_ICP, **{k: v for k, v in _load_icp_overrides().items() if v}}
TIER1_RE = re.compile(_ICP["tier1"], re.I)
TIER2_RE = re.compile(_ICP["tier2"], re.I)
EXCLUDE_ICP_RE = re.compile(_ICP["exclude"], re.I)
THEME_MAP = [(re.compile(p, re.I), lbl) for p, lbl in _ICP["themes"]]
HIRING_RE = re.compile(_ICP["hiring"], re.I)


def norm(s): return " ".join((s or "").strip().lower().split())


def match_norm(s):
    """Lowercase, drop apostrophes (straight + curly variants), collapse whitespace — so keyword
    substring matching is robust to punctuation. e.g. a term without an apostrophe then matches
    both the straight-quote and curly-quote forms (and the full-word form)."""
    s = (s or "").lower().replace("’", "").replace("ʼ", "").replace("'", "")
    return " ".join(s.split())


def is_excluded_company(name, exclude_set):
    """Exact normalized match against the alias set (avoids 'compa' matching 'company')."""
    return bool(name) and norm(name) in exclude_set


def own_company_engager(competitor, *companies):
    """True if the engager works at the same company whose post they engaged with — engaging with
    your own employer's content is not a competitive-buying signal. Handles 'Exec Name (Company)'
    competitor labels (e.g. 'Exec Name (Competitor One)') by matching on the parenthetical company."""
    comp = norm(competitor)
    if not comp:
        return False
    m = re.search(r"\(([^)]+)\)", competitor or "")
    target = norm(m.group(1)) if m else comp
    if len(target) < 4:
        return False
    for co in companies:
        c = norm(co)
        if c and (c == comp or c == target or re.search(rf"\b{re.escape(target)}\b", c)):
            return True
    return False


def load_config():
    path = os.path.join(REPO, "config", "targets.json")
    if not os.path.exists(path):
        sys.exit("ERROR: config/targets.json not found. Copy config/targets.example.json and fill it.")
    with open(path) as f:
        return json.load(f)


def load_watchlist_approved():
    """Approved bait-creators (config/watchlist.json) whose engagers we harvest going forward.
    Only the human-approved list is read here — candidates never auto-join."""
    path = os.path.join(REPO, "config", "watchlist.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("approved", [])


def out_dir():
    d = os.environ.get("COMP_INTEL_RAW_DIR") or os.path.join(REPO, "data", "raw")
    os.makedirs(os.path.expanduser(d), exist_ok=True)
    return os.path.expanduser(d)


def harvest_get(endpoint, params, attempts=4):
    """One GET against a HarvestAPI endpoint. Returns the parsed JSON envelope
    ({elements, pagination, status, ...} for list endpoints; {element, ...} for /profile).
    Counts the call per endpoint (cost/logging) and retries transient errors with backoff."""
    key = os.environ.get("HARVEST_API_KEY")
    if not key:
        sys.exit("ERROR: HARVEST_API_KEY not set (see .env).")
    CALL_COUNTS[endpoint] = CALL_COUNTS.get(endpoint, 0) + 1
    qs = urllib.parse.urlencode({k: v for k, v in params.items()
                                 if v not in (None, "", [])}, doseq=True)
    url = f"{HARVEST_BASE}{endpoint}?{qs}"
    for i in range(attempts):
        # A browser-like User-Agent is required: the default urllib UA ("Python-urllib/x.y")
        # trips HarvestAPI's Cloudflare bot filter (HTTP 403, error 1010 "access denied based on
        # browser signature"). Auth is still the X-API-Key header.
        req = urllib.request.Request(url, headers={
            "X-API-Key": key, "Accept": "application/json",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36")})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            transient = e.code in (429, 500, 502, 503, 504)
            if transient and i < attempts - 1:
                print(f"[harvest] {endpoint} {e.code}, retrying ({i+1}/{attempts})…",
                      file=sys.stderr)
                time.sleep(2 ** i)
                continue
            sys.exit(f"ERROR: HarvestAPI {endpoint} returned {e.code}: {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            if i < attempts - 1:
                print(f"[harvest] {endpoint} url error, retrying ({i+1}/{attempts})…",
                      file=sys.stderr)
                time.sleep(2 ** i)
                continue
            sys.exit(f"ERROR: HarvestAPI {endpoint} url error: {e}")


def harvest_paginate(endpoint, params, max_items=None):
    """Walk a paginated list endpoint to exhaustion, yielding items from each page's `elements`.
    Handles both the page-only endpoints (reactions, job-search) and the paginationToken ones
    (posts, comments): we always increment `page` and forward any `paginationToken` the previous
    page returned. Stops at totalPages, when a page returns no elements, or at max_items."""
    page, token, out = 1, None, []
    while True:
        p = dict(params)
        p["page"] = page
        if token:
            p["paginationToken"] = token
        data = harvest_get(endpoint, p)
        els = data.get("elements") or []
        out.extend(els)
        if max_items is not None and len(out) >= max_items:
            return out[:max_items]
        if not els:
            break
        pg = data.get("pagination") or {}
        token = pg.get("paginationToken")
        total = pg.get("totalPages")
        if total is not None and page >= total:
            break
        if total is None and not token:  # no more pages advertised and no cursor -> done
            break
        page += 1
    return out


def parse_company(headline):
    """Infer company from a headline like 'VP Operations at Acme' / 'Head of Ops @ Beta'."""
    m = re.search(r"(?:\bat\b|@)\s*(.+)$", headline or "", re.I)
    return m.group(1).strip() if m else ""


def is_non_icp(title):
    """Cheap pre-enrichment drop on the noisy headline. A Tier-1/2 signal WINS — so an
    incidental excluded word ('developer'/'coach') in a CPO/HRBP headline doesn't drop them."""
    t = title or ""
    if TIER1_RE.search(t) or TIER2_RE.search(t):
        return False
    return bool(EXCLUDE_ICP_RE.search(t))


def is_icp(title):
    """KEEP gate: the title must positively match Tier 1 or Tier 2.
    Tier presence is sufficient (a real target title wins over incidental exclude words);
    no Tier match -> dropped. Applied on the enriched real title."""
    t = title or ""
    return bool(TIER1_RE.search(t) or TIER2_RE.search(t))


# Engagement-bait = a comment/DM call-to-action PAIRED WITH a promised deliverable.
# (Standalone "👇" or the word "comment" are NOT bait on their own — too common.)
BAIT_CTA_RE = re.compile(
    r"(comment\s+(?:[\"'‘“]|below\b|the\s+word\b|\w+\s+(?:below|and|to\b|if\b))|"
    r"drop\s+(?:a|your|an)\b|\bdm\s+me\b|send\s+me\s+a\s+dm|type\s+[\"'\w])", re.I)
BAIT_DELIVERABLE_RE = re.compile(
    r"(i'?ll\s+(?:send|share|dm|drop)|send\s+you\s+the|share\s+the\b|"
    r"link\s+in\s+(?:the\s+)?comments|want\s+(?:the|a|my)\s+\w+|get\s+(?:the|a|my)\s+\w+|"
    r"send\s+(?:it|the)\b|i'?ll\s+(?:email|message))", re.I)


def is_bait(text):
    """True only when a post has BOTH a comment/DM call-to-action AND a promised deliverable
    ('comment WORD and I'll send you the template'). Hiring posts are excluded by the caller."""
    t = text or ""
    return bool(BAIT_CTA_RE.search(t) and BAIT_DELIVERABLE_RE.search(t))


def summarize_topic(text, cache):
    """Short Post Topic label: deterministic theme map first; Haiku only for misses (cached)."""
    text = (text or "").strip()
    if not text:
        return ""
    for rx, label in THEME_MAP:
        if rx.search(text):
            return label
    key = text[:200]
    if key in cache:
        return cache[key]
    topic = _haiku_topic(text)
    if not topic:  # no key or call failed -> short snippet fallback
        topic = " ".join(text.split()[:6])
    cache[key] = topic
    return topic


def _haiku_topic(text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return ""
    body = {"model": ANTHROPIC_MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content":
                          "Reply with ONLY a 2-4 word topic label (no punctuation, no quotes) "
                          "for this LinkedIn post:\n\n" + text[:1500]}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        return (data.get("content", [{}])[0].get("text") or "").strip().strip('"')[:60]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, IndexError) as e:
        print(f"[topic] LLM fallback failed: {e}", file=sys.stderr)
        return ""


# ----------------------------- engagement track -----------------------------

def _slug_of(url):
    """LinkedIn slug (company or profile) from a page URL: the segment after /company/ or /in/."""
    m = re.search(r"/(?:company|in)/([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def fetch_target_posts(url, posted_limit, scan):
    """Fetch up to `scan` recent posts for one target via the direct API, choosing the endpoint by
    URL type: /company/… -> company-posts (?company=), a profile URL -> profile-posts (?profile=).
    Returns the raw `elements` (PostShort items)."""
    if "/company/" in (url or ""):
        return harvest_paginate(EP_COMPANY_POSTS,
                                {"company": url, "postedLimit": posted_limit}, max_items=scan)
    return harvest_paginate(EP_PROFILE_POSTS,
                            {"profile": url, "postedLimit": posted_limit}, max_items=scan)


def post_engagement_score(post):
    """Local engagement rank key: likes + comments + shares from the post's `engagement` block."""
    e = post.get("engagement") or {}
    return sum(int(e.get(k) or 0) for k in ("likes", "comments", "shares"))


def run_engagement(cfg, test):
    eng = cfg.get("engagement", {})
    drop = {norm(u) for u in eng.get("drop_list", [])}
    exclude = {norm(x) for x in eng.get("exclude_engager_companies", [])}
    exclude_hiring = eng.get("exclude_hiring_posts", True)
    targets = [u for u in (eng.get("competitor_company_urls", []) + eng.get("competitor_exec_urls", []))
               if norm(u) not in drop]
    labels = dict(eng.get("competitor_label", {}))
    # Track 3 feedback: approved bait-creators feed future engagement runs.
    if eng.get("include_approved_watchlist", True):
        for a in load_watchlist_approved():
            u = a.get("url", "")
            if u and norm(u) not in drop and u not in targets:
                targets.append(u)
                labels.setdefault(u, a.get("author", ""))
    if not targets:
        print("engagement: no targets after drop-list; skipping", file=sys.stderr)
        return None

    posted_limit = eng.get("posted_limit", "week")
    top_n = 3 if test else eng.get("posts_per_target", 5)            # posts we pull engagers for
    scan = 5 if test else eng.get("posts_scan_per_target", max(top_n * 2, 10))  # ranking pool
    max_reactions = 15 if test else eng.get("max_reactions_per_post", 30)
    max_comments = 10 if test else eng.get("max_comments_per_post", 20)
    target_terms = [t.lower() for t in cfg.get("bait_discovery", {}).get("target_terms", [])]
    topic_cache = {}

    slug_label = {}
    for url, label in labels.items():
        s = _slug_of(url)
        if s:
            slug_label[s] = label

    def topic_score(text):  # how strongly a post matches the target topic
        low = (text or "").lower()
        return sum(1 for t in target_terms if t in low)

    def author_competitor(author):
        """Competitor label for a post's author: a tracked target's slug -> its label; empty for
        a non-target author (e.g. a reshared/original post surfaced in the target's feed)."""
        for s in (str(author.get("publicIdentifier") or "").lower(),
                  str(author.get("universalName") or "").lower(),
                  _slug_of(author.get("linkedinUrl") or "")):
            if s and s in slug_label:
                return slug_label[s]
        return ""

    # PASS 1 — per target, fetch & CLASSIFY posts, then rank by engagement and keep the top-N
    # SURFACE-able, non-hiring posts. Reactions/comments are fetched only for those selected posts
    # (the whole point of the split API: rank cheaply on post text, spend on engagers selectively).
    posts, review = {}, []
    n_scanned = 0
    for turl in targets:
        scanned = fetch_target_posts(turl, posted_limit, scan)
        n_scanned += len(scanned)
        ranked = sorted(scanned, key=post_engagement_score, reverse=True)
        kept = 0
        for it in ranked:
            if kept >= top_n:
                break
            pid = str(it.get("id"))
            if not pid or pid in posts:
                continue
            url = it.get("linkedinUrl") or ""
            author = it.get("author") or {}
            text = it.get("content") or ""
            competitor = author_competitor(author)
            surface = True
            if not competitor:  # non-target author (e.g. a post a target reshared)
                cs = topic_score(text)
                if cs >= 2:
                    competitor = (author.get("name") or "") + " (discovered)"
                elif cs == 1:
                    surface = False
                    review.append({"author": author.get("name") or "",
                                   "url": author.get("linkedinUrl") or "", "post_url": url,
                                   "target_terms_hit": cs, "sample": text[:200], "status": "review"})
                else:
                    surface = False  # not on-target -> drop
            hiring = bool(exclude_hiring and text and HIRING_RE.search(text))
            posts[pid] = {"text": text, "competitor": competitor, "url": url, "surface": surface,
                          "hiring": hiring, "bait": is_bait(text),
                          "topic": summarize_topic(text, topic_cache) if surface else ""}
            if surface and not hiring:
                kept += 1  # only surface-able posts count toward the top-N engager budget

    selected = [pid for pid, m in posts.items() if m["surface"] and not m["hiring"]]
    sk_hire = sum(1 for m in posts.values() if m["hiring"])
    sk_nontarget = sum(1 for m in posts.values() if not m["surface"])

    def is_company_actor(a):  # company pages sometimes appear as engagers — not leads
        return ("/company/" in (a.get("linkedinUrl") or "")
                or bool(re.search(r"\d[\d,]*\s+followers", a.get("position") or "", re.I)))

    # PASS 2 — for each SELECTED post, fetch reactions + comments (paginated to the cap) and build
    # one flattened engagement event per engager, then apply the same ICP/dedupe/hand-raiser logic.
    events = []
    for pid in selected:
        purl = posts[pid]["url"]
        if not purl:
            continue
        for r in harvest_paginate(EP_POST_REACTIONS, {"post": purl}, max_items=max_reactions):
            events.append({"type": "reaction", "pid": pid, "actor": r.get("actor") or {}})
        for c in harvest_paginate(EP_POST_COMMENTS, {"post": purl}, max_items=max_comments):
            events.append({"type": "comment", "pid": pid, "actor": c.get("actor") or {},
                           "commentary": c.get("commentary") or ""})

    rows = {}
    sk_co = sk_page = sk_icp = 0
    for it in events:
        etype = it["type"]
        pid = it["pid"]
        meta = posts.get(pid, {})
        a = it.get("actor") or {}
        name = a.get("name") or a.get("fullName") or ""
        headline = a.get("position") or a.get("headline") or a.get("occupation") or ""
        if not name or is_company_actor(a):
            sk_page += (1 if name else 0)
            continue
        if is_non_icp(headline):  # obvious non-ICP (engineer/data/sales/...) — drop pre-enrich
            sk_icp += 1
            continue
        company = parse_company(headline)
        if is_excluded_company(company, exclude):
            sk_co += 1
            continue
        hand_raiser = "Y" if (etype == "comment" and meta.get("bait")) else "N"
        row = {
            "Engager Name": name, "Engager Company": company, "Title": headline,
            "Email": "", "Current Company": "",
            "Competitor": meta.get("competitor", ""),
            "Competitor Post Topic": meta.get("topic", ""),
            "Post Type": etype, "Hand Raiser": hand_raiser,
            "Post URL": meta.get("url") or "", "Domain": "",
            "_url": a.get("linkedinUrl") or "",  # transient: engager profile URL for enrichment
        }
        key = (norm(name), norm(headline)[:40], pid)
        prev = rows.get(key)
        if prev is None or (etype == "comment" and prev["Post Type"] == "reaction"):
            rows[key] = row

    out_rows = list(rows.values())
    if eng.get("enrich_current_company", True):
        enrich_current_company(out_rows)  # sets real Title + Company + Current Company
        before = len(out_rows)
        # Positive ICP gate on the enriched real title (drops PT/CSM/GTM/CEO/etc.), plus the
        # competitor/own-employer exclusion that enrichment may now reveal.
        out_rows = [r for r in out_rows
                    if is_icp(r["Title"]) and not is_excluded_company(r["Current Company"], exclude)
                    and not own_company_engager(r.get("Competitor", ""),
                                                r.get("Current Company", ""), r.get("Engager Company", ""))]
        sk_co += before - len(out_rows)
    for r in out_rows:
        r["Profile URL"] = r.pop("_url", "")   # persist engager profile URL for later re-enrichment

    if review:
        _write_review(review)
    print(f"engagement: kept {len(out_rows)} | scanned {n_scanned} posts, "
          f"selected {len(selected)} for engagers | skipped hiring={sk_hire} competitor={sk_co} "
          f"pages={sk_page} non-ICP={sk_icp} non-target={sk_nontarget} | review={len(review)}",
          file=sys.stderr)
    print("engagement: API calls -> " + " ".join(
        f"{ep.rsplit('/', 1)[-1]}={CALL_COUNTS.get(ep, 0)}"
        for ep in (EP_COMPANY_POSTS, EP_PROFILE_POSTS, EP_POST_REACTIONS,
                   EP_POST_COMMENTS, EP_PROFILE)), file=sys.stderr)
    return write_csv("engagement", ENGAGEMENT_HEADER, out_rows)


def _write_review(review):
    """Append non-target posts (low confidence) to config/review_candidates.json for
    human review — same gate pattern as the bait watchlist; nothing auto-surfaces."""
    path = os.path.join(REPO, "config", "review_candidates.json")
    existing = []
    if os.path.exists(path):
        try:
            existing = json.load(open(path)).get("posts", [])
        except (ValueError, OSError):
            existing = []
    seen = {r.get("post_url") for r in existing}
    existing += [r for r in review if r.get("post_url") not in seen]
    with open(path, "w") as f:
        json.dump({"posts": existing}, f, indent=2, ensure_ascii=False)


def enrich_current_company(rows):
    """Fill the real **Title** + **Company** by looking up each unique engager profile via the
    direct /linkedin/profile endpoint. It fills the actual current job title
    (currentPosition[0].position, falling back to experience[0].position) and current company
    (currentPosition[0].companyName / experience[0].companyName) — not the noisy headline.
    Email is intentionally NOT fetched (findEmail is left off). One GET per unique engager URL
    (the direct API does not batch), so results are cached by URL across rows."""
    def nurl(u):  # normalize for matching / caching
        return (u or "").split("?")[0].rstrip("/").lower()

    urls = sorted({nurl(r.get("_url", "")) for r in rows if r.get("_url", "")})
    if not urls:
        return rows
    info_by = {}  # nurl -> {"company":..., "title":...}
    for u in urls:
        data = harvest_get(EP_PROFILE, {"url": u})
        p = data.get("element") or {}
        cp = p.get("currentPosition") or []
        exp = p.get("experience") or []
        cp0 = cp[0] if cp and isinstance(cp[0], dict) else {}
        exp0 = exp[0] if exp and isinstance(exp[0], dict) else {}
        info_by[u] = {
            "company": cp0.get("companyName") or exp0.get("companyName") or "",
            "title": cp0.get("position") or exp0.get("position") or "",
        }

    filled = 0
    for r in rows:
        info = info_by.get(nurl(r.get("_url", "")))
        if not info:
            continue
        if info["company"]:
            r["Current Company"] = info["company"]
            r["Engager Company"] = info["company"]   # the single meaningful Company for the view
            filled += 1
        if info["title"]:
            r["Title"] = info["title"]               # real job title, not the headline
    print(f"enrichment: company/title filled for {filled}/{len(rows)} engager(s) "
          f"({len(urls)} profile lookups)", file=sys.stderr)
    return rows


# ----------------------------- jobs track -----------------------------

def run_jobs(cfg, test):
    jobs = cfg.get("jobs", {})
    titles = jobs.get("titles", [])
    if not titles:
        print("jobs: no titles configured; skipping", file=sys.stderr)
        return None
    locations = jobs.get("locations", ["United States"])
    rel = [match_norm(k) for k in jobs.get("relevance_keywords", [])]
    excl_titles = [match_norm(k) for k in jobs.get("exclude_title_keywords", [])]
    # Reuse the engagement competitor/own-company list: a competitor hiring target roles is
    # not a buying signal for us.
    excl_co = ({norm(x) for x in cfg.get("engagement", {}).get("exclude_engager_companies", [])}
               if jobs.get("exclude_competitor_companies", True) else set())
    max_per = 5 if test else jobs.get("max_per_title", 25)
    posted_limit = jobs.get("posted_limit", "week")
    sort_by = jobs.get("sort_by", "date")   # 'date' (freshest first) or 'relevance'

    # One row per posting. Same posting surfaced by multiple title-searches is deduped by
    # (company, role, url); two genuinely-distinct postings of the same role -> separate rows.
    # The direct job-search endpoint takes ONE title + ONE location per query, so we loop the
    # title x location grid and paginate each to `max_per`.
    out_rows, seen = [], set()
    fetched = dropped_rel = dropped_title = dropped_co = 0
    for title in titles:
        for location in locations:
            for job in harvest_paginate(EP_JOB_SEARCH,
                                        {"search": title, "location": location,
                                         "postedLimit": posted_limit, "sortBy": sort_by},
                                        max_items=max_per):
                fetched += 1
                jt = job.get("title") or ""
                jt_n = match_norm(jt)
                if rel and not any(k and k in jt_n for k in rel):
                    dropped_rel += 1
                    continue
                if excl_titles and any(k and k in jt_n for k in excl_titles):  # sales, clinical, ...
                    dropped_title += 1
                    continue
                co = job.get("company")
                company = (co.get("name") if isinstance(co, dict) else co) or ""
                # The job-search item does NOT expose a company web domain (only name /
                # universalName / linkedinUrl), so Domain stays blank for the jobs track and the
                # CRM match/route step resolves it by company name. (`website` is read defensively
                # in case a future response includes it.)
                website = co.get("website") if isinstance(co, dict) else ""
                if not company:
                    continue
                if is_excluded_company(company, excl_co):  # competitor hiring — not a buyer
                    dropped_co += 1
                    continue
                url = job.get("url") or job.get("linkedinUrl") or ""
                key = (norm(company), norm(jt), url)
                if key in seen:
                    continue
                seen.add(key)
                out_rows.append({"Company": company, "Domain": domain_from(website or ""),
                                 "Signal": "target-role hiring",
                                 "Job Titles": jt, "Post URL": url})
    print(f"jobs: {len(out_rows)} posting(s) kept | {fetched} fetched from API; dropped "
          f"{dropped_rel} off-topic, {dropped_title} excluded-title, {dropped_co} competitor "
          f"| job-search calls={CALL_COUNTS.get(EP_JOB_SEARCH, 0)}", file=sys.stderr)
    return write_csv("jobs", JOBS_HEADER, out_rows)


def domain_from(url):
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


# ----------------------------- shared -----------------------------

def write_csv(track, header, rows):
    fn = os.path.join(out_dir(), f"{track}_{date.today().isoformat()}.csv")
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    print(f"{track}: wrote {len(rows)} rows -> {fn}", file=sys.stderr)
    return fn


def estimate(cfg):
    """Offline UPPER-BOUND projection of HarvestAPI credit spend (no API calls). Bills per
    returned result at the direct-API pay-as-you-go rate (no Apify platform margin). Actual spend
    is lower — ICP/dedupe filtering trims engager and profile-lookup counts, and targets often
    have fewer posts/engagers than the caps."""
    price = {**DEFAULT_PRICING, **{k: v for k, v in (cfg.get("pricing") or {}).items()
                                   if isinstance(v, (int, float))}}
    eng = cfg.get("engagement", {})
    n_targets = len(eng.get("competitor_company_urls", []) + eng.get("competitor_exec_urls", []))
    top_n = eng.get("posts_per_target", 5)
    scan = eng.get("posts_scan_per_target", max(top_n * 2, 10))
    react = eng.get("max_reactions_per_post", 30)
    comm = eng.get("max_comments_per_post", 20)
    posts = n_targets * scan                      # posts fetched for ranking
    selected = n_targets * top_n                  # posts we pull engagers for
    reactions = selected * react
    comments = selected * comm
    profiles = (reactions + comments) if eng.get("enrich_current_company", True) else 0

    jobs = cfg.get("jobs", {})
    job_results = len(jobs.get("titles", [])) * len(jobs.get("locations", ["United States"])) \
        * jobs.get("max_per_title", 25)

    eng_cost = (posts * price["post"] + reactions * price["reaction"]
                + comments * price["comment"] + profiles * price["profile"])
    job_cost = job_results * price["job"]
    print(f"[estimate] engagement: {n_targets} targets -> ~{posts} posts scanned, "
          f"~{selected} selected x ({react} reactions + {comm} comments) = "
          f"~{reactions + comments} engagers; ~{profiles} profile lookups")
    print(f"[estimate] jobs: ~{job_results} job results")
    print(f"[estimate] projected HarvestAPI cost (upper bound): "
          f"engagement ${eng_cost:.2f} + jobs ${job_cost:.2f} = ${eng_cost + job_cost:.2f}")
    print(f"[estimate] rate: ${price['post']:.4f}/result (confirm your plan at "
          f"harvestapi.io/pricing; override via a \"pricing\" block in config/targets.json)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=["engagement", "jobs", "both"], default="both")
    ap.add_argument("--test", action="store_true", help="small caps, live smoke test")
    ap.add_argument("--estimate-only", action="store_true", help="offline cost estimate, no API calls")
    a = ap.parse_args()

    cfg = load_config()
    if a.estimate_only:
        estimate(cfg)
        return

    written = []
    if a.track in ("engagement", "both"):
        f = run_engagement(cfg, a.test)
        if f: written.append(f)
    if a.track in ("jobs", "both"):
        f = run_jobs(cfg, a.test)
        if f: written.append(f)
    print(f"scrape complete: {len(written)} file(s) in {out_dir()}", file=sys.stderr)


if __name__ == "__main__":
    main()
