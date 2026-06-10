#!/usr/bin/env python3
"""
Weekly competitive-intent scrape (local machine). Two tracks via Apify harvestapi actors:

  engagement -> harvestapi/linkedin-company-posts  (reactors + commenters on competitor posts)
  jobs       -> harvestapi/linkedin-job-search     (target-role hiring signals)

Output: normalize-ready CSVs dropped into the staging folder (COMP_INTEL_RAW_DIR, normally
the local path of the Drive-for-Desktop 'comp-intel-raw' folder). The column
headers are chosen to match ingest/normalize.py's ENGAGEMENT_MAP / JOB_MAP, so the files feed
straight into the engine with no further mapping.

This script NEVER touches the CRM and NEVER writes to the sheet — it only produces raw files.
It does NOT change the Master schema — it just populates the existing columns better and
filters rows.

Engagement filters/derivations: drop hiring posts, competitor/own employees, company pages,
and obvious non-ICP titles; keep only target-authored posts (non-target posts go to a
review list, or surface if clearly on-target). Title + Company come from profile enrichment;
Post Topic is a short themed label (Haiku fills the misses); Hand Raiser = comment on a
bait post; engagers on a target exec's posts get that exec's Competitor label.

Config: config/targets.json (gitignored; copy from config/targets.example.json).
Secrets: APIFY_API_TOKEN (required), ANTHROPIC_API_KEY (optional, topic summarizer) — from .env.

Usage:
    python3 scripts/scrape.py --estimate-only          # offline cost estimate, no API calls
    python3 scripts/scrape.py --test                   # small caps, live, smoke test
    python3 scripts/scrape.py                           # full run, both tracks
    python3 scripts/scrape.py --track jobs              # one track only
"""
import argparse, csv, json, os, re, sys, time, urllib.request, urllib.error
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APIFY_BASE = "https://api.apify.com/v2/acts"
ENGAGEMENT_ACTOR = "harvestapi~linkedin-company-posts"
JOBS_ACTOR = "harvestapi~linkedin-job-search"
PROFILE_ACTOR = "harvestapi~linkedin-profile-scraper"
PROFILE_MODE = "Profile details no email ($4 per 1k)"  # current company, no email

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


def apify_run(actor, payload, attempts=3):
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        sys.exit("ERROR: APIFY_API_TOKEN not set (see .env).")
    url = f"{APIFY_BASE}/{actor}/run-sync-get-dataset-items?token={token}"
    for i in range(attempts):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            transient = e.code in (429, 500, 502, 503, 504)
            if transient and i < attempts - 1:
                print(f"[apify] {actor} {e.code}, retrying ({i+1}/{attempts})…", file=sys.stderr)
                time.sleep(5 * (i + 1))
                continue
            sys.exit(f"ERROR: Apify {actor} returned {e.code}: {e.read().decode()[:300]}")
        except urllib.error.URLError as e:
            if i < attempts - 1:
                print(f"[apify] {actor} url error, retrying ({i+1}/{attempts})…", file=sys.stderr)
                time.sleep(5 * (i + 1))
                continue
            sys.exit(f"ERROR: Apify {actor} url error: {e}")


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
    payload = {
        "targetUrls": targets,
        "postedLimit": eng.get("posted_limit", "week"),
        "maxPosts": 3 if test else eng.get("posts_per_target", 5),
        "scrapeReactions": True,
        "scrapeComments": True,
        "maxReactions": 15 if test else eng.get("max_reactions_per_post", 30),
        "maxComments": 10 if test else eng.get("max_comments_per_post", 20),
    }
    items = apify_run(ENGAGEMENT_ACTOR, payload)
    target_terms = [t.lower() for t in cfg.get("bait_discovery", {}).get("target_terms", [])]
    topic_cache = {}

    # Actor returns FLATTENED records (type in {post, reaction, comment}); `actor` = engager.
    slug_label = {}
    for url, label in labels.items():
        m = re.search(r"/(?:company|in)/([^/?#]+)", url)
        if m:
            slug_label[m.group(1).lower()] = label

    def competitor_from_url(url):
        m = re.search(r"/posts/([^_/?#]+)_", url or "")  # /posts/<authorslug>_...
        return slug_label.get(m.group(1).lower(), "") if m else ""

    def topic_score(text):  # how strongly a post matches the target topic
        low = (text or "").lower()
        return sum(1 for t in target_terms if t in low)

    # Pass 1: index posts. Link engagement ids (commentIds/reactionIds) -> post id (sidesteps
    # the ugcPost/activity twin mismatch). Classify target vs non-target; for non-target
    # posts: surface engagers if clearly on-target (>=2 terms), else send the post to the review list.
    posts, eng_to_post, review = {}, {}, []
    for it in items:
        if it.get("type") != "post":
            continue
        pid = str(it.get("id"))
        url = it.get("linkedinUrl") or ""
        author = it.get("author") or {}
        text = it.get("content") or ""
        competitor = (competitor_from_url(url)
                      or slug_label.get(str(author.get("universalName") or "").lower(), "")
                      or slug_label.get(str(author.get("publicIdentifier") or "").lower(), ""))
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
        posts[pid] = {"text": text, "competitor": competitor, "url": url, "surface": surface,
                      "bait": is_bait(text),
                      "topic": summarize_topic(text, topic_cache) if surface else ""}
        for eid in (it.get("commentIds") or []) + (it.get("reactionIds") or []):
            eng_to_post[str(eid)] = pid

    hiring_keys = {pid for pid, m in posts.items()
                   if exclude_hiring and m["text"] and HIRING_RE.search(m["text"])}

    def is_company_actor(a):  # company pages sometimes appear as engagers — not leads
        return ("/company/" in (a.get("linkedinUrl") or "")
                or bool(re.search(r"\d[\d,]*\s+followers", a.get("position") or "", re.I)))

    rows = {}
    sk_hire = sk_co = sk_page = sk_icp = sk_nontarget = 0
    for it in items:
        etype = it.get("type")
        if etype not in ("reaction", "comment"):
            continue
        pid = eng_to_post.get(str(it.get("id"))) or str(it.get("postId"))
        meta = posts.get(pid, {})
        if not meta.get("surface", True):
            sk_nontarget += 1
            continue
        if pid in hiring_keys:
            sk_hire += 1
            continue
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
            "Post URL": meta.get("url") or it.get("linkedinUrl") or "", "Domain": "",
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
    print(f"engagement: kept {len(out_rows)} | skipped hiring={sk_hire} competitor={sk_co} "
          f"pages={sk_page} non-ICP={sk_icp} non-target={sk_nontarget} | review={len(review)}",
          file=sys.stderr)
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
    """Fill the real **Title** + **Company** by bulk-scraping each unique engager profile URL
    via harvestapi/linkedin-profile-scraper ('no email' mode, $4/1k — reuses APIFY_API_TOKEN).
    Uses currentPosition[0].position (actual job title, not the noisy headline) and .companyName.
    Email is intentionally NOT fetched. One actor run per chunk of URLs."""
    urls = sorted({r.get("_url", "") for r in rows if r.get("_url", "")})
    if not urls:
        return rows
    info_by = {}  # key -> {"company":..., "title":...}

    def nurl(u):  # normalize for matching
        return (u or "").split("?")[0].rstrip("/").lower()

    def pid_of(u):
        m = re.search(r"/in/([^/?#]+)", u or "")
        return m.group(1).lower() if m else ""

    def url_keys(p):
        keys = []
        for cand in (p.get("originalQuery"), p.get("linkedinUrl")):
            if isinstance(cand, str):
                keys.append(cand)
            elif isinstance(cand, dict):  # originalQuery is an object echoing the input
                for vk in ("url", "linkedinUrl", "profileUrl", "query"):
                    if isinstance(cand.get(vk), str):
                        keys.append(cand[vk])
        return keys

    CHUNK = 100
    for i in range(0, len(urls), CHUNK):
        items = apify_run(PROFILE_ACTOR,
                          {"urls": urls[i:i + CHUNK], "profileScraperMode": PROFILE_MODE})
        for p in items:
            cp = p.get("currentPosition") or p.get("experience") or []
            cp0 = cp[0] if cp and isinstance(cp[0], dict) else {}
            info = {"company": cp0.get("companyName") or "", "title": cp0.get("position") or ""}
            if not (info["company"] or info["title"]):
                continue
            for k in url_keys(p):
                info_by[nurl(k)] = info
            if p.get("publicIdentifier"):
                info_by["pid:" + p["publicIdentifier"].lower()] = info

    filled = 0
    for r in rows:
        u = r.get("_url", "")
        info = info_by.get(nurl(u)) or info_by.get("pid:" + pid_of(u))
        if not info:
            continue
        if info["company"]:
            r["Current Company"] = info["company"]
            r["Engager Company"] = info["company"]   # the single meaningful Company for the view
            filled += 1
        if info["title"]:
            r["Title"] = info["title"]               # real job title, not the headline
    print(f"enrichment: company/title filled for {filled}/{len(rows)} engager(s)", file=sys.stderr)
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

    # One row per posting. Same posting surfaced by multiple title-searches is deduped by
    # (company, role, url); two genuinely-distinct postings of the same role -> separate rows.
    out_rows, seen = [], set()
    fetched = dropped_rel = dropped_title = dropped_co = 0
    for title in titles:
        payload = {"jobTitles": [title], "locations": locations,
                   "maxItems": max_per, "postedLimit": jobs.get("posted_limit", "week")}
        for job in apify_run(JOBS_ACTOR, payload):
            fetched += 1
            jt = job.get("title") or ""
            jt_n = match_norm(jt)
            if rel and not any(k and k in jt_n for k in rel):
                dropped_rel += 1
                continue
            if excl_titles and any(k and k in jt_n for k in excl_titles):  # sales, clinical, legal, etc.
                dropped_title += 1
                continue
            co = job.get("company")
            company = (co.get("name") if isinstance(co, dict) else co) or ""
            website = co.get("website") if isinstance(co, dict) else ""
            if not company:
                continue
            if is_excluded_company(company, excl_co):  # competitor hiring — not a buyer
                dropped_co += 1
                continue
            url = job.get("linkedinUrl") or job.get("url") or ""
            key = (norm(company), norm(jt), url)
            if key in seen:
                continue
            seen.add(key)
            out_rows.append({"Company": company, "Domain": domain_from(website or ""),
                             "Signal": "target-role hiring",
                             "Job Titles": jt, "Post URL": url})
    print(f"jobs: {len(out_rows)} posting(s) kept | {fetched} fetched from actor; dropped "
          f"{dropped_rel} off-topic, {dropped_title} excluded-title, {dropped_co} competitor",
          file=sys.stderr)
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
    eng = cfg.get("engagement", {})
    n_targets = len(eng.get("competitor_company_urls", []) + eng.get("competitor_exec_urls", []))
    posts = eng.get("posts_per_target", 5)
    react = eng.get("max_reactions_per_post", 30)
    comm = eng.get("max_comments_per_post", 20)
    eng_events = n_targets * posts * (react + comm)
    jobs = cfg.get("jobs", {})
    job_events = len(jobs.get("titles", [])) * len(jobs.get("locations", ["United States"])) \
        * jobs.get("max_per_title", 25)
    # harvestapi pay-per-event ~ $0.001/event + 20% platform fee (see skill docs).
    cost = (eng_events + job_events) * 0.001 * 1.2
    print(f"[estimate] engagement: {n_targets} targets x {posts} posts x ~{react+comm} engagers "
          f"= ~{eng_events} events")
    print(f"[estimate] jobs: ~{job_events} job results")
    print(f"[estimate] approx Apify cost: ${cost:.2f} (rough; confirm in console.apify.com/billing)")


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
