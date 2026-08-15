#!/usr/bin/env python3
"""
Gift-research pipeline (HarvestAPI direct): find Total Rewards / Compensation leaders
at US tech companies who have a volunteer affiliation or membership org on their
LinkedIn profile, then corroborate how ACTIVELY involved they are — so a donation in
their honor lands as thoughtful, not random.

Stages (each checkpointed in data/gift/state.json — re-runs never re-pay for done work):
  1. leads    — /linkedin/lead-search: persona titles x companyHeadcount x US x tech
                industries ($0.10/page of ~25).
  2. gate     — local, free: title must contain a comp/TR phrase AND be Senior
                Manager+; analysts/coordinators/specialists are cut.
  3. profiles — /linkedin/profile per gated lead (~$0.0064): extract volunteering /
                organizations / causes. No affiliation -> cut (recorded in audit).
  4. classify — local, free: flag religious / political / health orgs (kept in the
                output, clearly labeled — default do-not-gift).
  5. activityA — the contact's own posts / comments / reactions (1 page each,
                ~$0.004/page): did THEY talk about or engage with the org?
  6. activityB — the org's side: resolve org -> company page -> recent posts ->
                reactions+comments per post: does the contact show up repeatedly?
                (org lookups cached and shared across contacts).
  7. score    — Tier A (current + engagement evidence or leadership role),
                Tier B (current, clean, but quiet), Tier C/cut (ended or stale).

Output: data/gift/gift_candidates_<date>.csv + gift_audit_<date>.csv (everyone cut,
with the reason). Costs are tallied and printed. NOTHING here touches a CRM, and the
output is a human-review list — no gifting/outreach is automated.

Secret: HARVESTAPI_API_KEY (from .env).

Usage:
    python3 scripts/gift_research.py --test          # 1 lead page, 5 profiles, live
    python3 scripts/gift_research.py                 # full run (default 500 leads)
    python3 scripts/gift_research.py --max-leads 250
"""
import argparse, csv, http.client, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # REPO path helper

API_BASE = "https://api.harvest-api.com"
GIFT_DIR = os.path.join(scrape.REPO, "data", "gift")
STATE_PATH = os.path.join(GIFT_DIR, "state.json")

# --- persona / ICP defaults (US tech, HC 501-10,000, Total Rewards & Comp SM+) ---
TITLES = ["Total Rewards", "Compensation", "Compensation and Benefits",
          "Head of Total Rewards", "Director of Total Rewards", "VP Total Rewards",
          "Director of Compensation", "Head of Compensation", "VP Compensation"]
HEADCOUNT = ["501-1000", "1001-5000", "5001-10000"]
LOCATIONS = ["United States"]
INDUSTRY_IDS = ["4", "6", "96"]  # Computer Software / Internet-Tech / IT Services

PERSONA_RE = re.compile(r"(total\s*rewards|compensation|comp\s*(&|and)\s*benefits)", re.I)
SENIOR_RE = re.compile(r"(senior\s*manager|sr\.?\s*manager|head\b|director|vice\s*president"
                       r"|\bvp\b|chief|\bc[a-z]o\b|president)", re.I)
EXCLUDE_TITLE_RE = re.compile(r"(analyst|coordinator|specialist|associate\b|intern\b|"
                              r"consultant|recruiter|assistant\b|"
                              # Adjacent comp disciplines that are NOT the buyer persona:
                              r"sales\s*comp|incentive\s*comp|sales\s*incentive|"
                              r"commission|workers'?\s*comp|workman'?s\s*comp)", re.I)

# Orgs never worth a donation-gift: huge trade associations, plus Greek-letter
# fraternities/sororities/honor societies (a donation there isn't a cause gift).
_GREEK = r"alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega"
ORG_BLOCKLIST_RE = re.compile(r"(world\s*at\s*work|worldatwork|\bshrm\b|"
                              r"society for human resource management|"
                              r"fraternit|sororit|honou?r\s*society|greek\s*life|"
                              rf"\b({_GREEK})\s+({_GREEK})(\s+({_GREEK}))?\b)", re.I)

# Meaningful involvement: an officer/founder/board-level role in the org.
LEADER_RE = re.compile(r"(found(er|ing)|co-?found|board|chair|president|trustee|"
                       r"treasurer|secretary|officer|organizer|\blead\b|captain|"
                       r"committee|advisor|adviser|coach|mentor|director)", re.I)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # org donation-fit judge (cheap, cached)

# The lead's employer must be verifiably tech/software (belt-and-braces on top of
# the server-side industry filter, which uses LinkedIn ids that can misclassify).
TECH_INDUSTRY_RE = re.compile(r"(software|technolog|internet|information technology|"
                              r"it services|computer|cloud|saas|data|cyber|"
                              r"artificial intelligence|semiconductor|fintech)", re.I)

# Sensitive-affiliation flags: kept in output, labeled, default do-not-gift.
SENSITIVE = {
    "religious": re.compile(r"(church|ministry|ministries|parish|temple|mosque|synagogue|"
                            r"faith|christ|catholic|baptist|jewish|islamic|hindu|buddhis|"
                            r"chaplain|mission(ary)?\b|gospel|bible|diocese|archdiocese|"
                            r"lutheran|methodist|presbyterian|evangel|salvation army|"
                            r"young life|ymca|ywca)", re.I),
    "political": re.compile(r"(democrat|republican|gop\b|political|campaign|pac\b|"
                            r"libert(y|arian)|progressive|conservative|activis|lobby)", re.I),
    "health":    re.compile(r"(cancer|als\b|alzheim|diabet|autism|lupus|sclerosis|"
                            r"leukemia|hospice|mental\s*health|suicide|addiction|recovery)", re.I),
}

PRICE = {"lead-search": 0.10, "profile": 0.0064, "profile-posts": 0.004,
         "profile-comments": 0.004, "profile-reactions": 0.004, "company-search": 0.004,
         "company-posts": 0.004, "post-reactions": 0.004, "post-comments": 0.004}
spend = {"requests": 0, "usd": 0.0}


def api_get(endpoint, params, attempts=3):
    key = os.environ.get("HARVESTAPI_API_KEY")
    if not key:
        sys.exit("ERROR: HARVESTAPI_API_KEY not set (see .env).")
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{API_BASE}/linkedin/{endpoint}?{qs}"
    for i in range(attempts):
        req = urllib.request.Request(url, headers={
            "X-API-Key": key,
            "User-Agent": "Mozilla/5.0 (compatible; comp-intel-hub/1.0)",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                spend["requests"] += 1
                spend["usd"] += PRICE.get(endpoint, 0.004)
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(5 * (i + 1)); continue
            if e.code not in (429, 500, 502, 503, 504):
                sys.exit(f"ERROR: HarvestAPI {endpoint} returned {e.code}: "
                         f"{e.read().decode()[:300]}")
            print(f"[harvest] {endpoint} {e.code} after retries — skipping", file=sys.stderr)
            return None
        except (urllib.error.URLError, http.client.HTTPException, OSError,
                json.JSONDecodeError) as e:
            if i < attempts - 1:
                time.sleep(5 * (i + 1)); continue
            print(f"[harvest] {endpoint} {type(e).__name__} — skipping", file=sys.stderr)
            return None


# ----------------------------- state / helpers -----------------------------

def load_state():
    st = {"leads": [], "leads_done": False, "profiles": {}, "activity": {},
          "org_cache": {}, "companies": {}}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            st.update(json.load(f))
    return st


def save_state(st):
    os.makedirs(GIFT_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(st, f)


def first(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("text") or ""
        if v:
            return v
    return default


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def org_tokens(org_name):
    """Distinctive tokens for matching an org in post text (drop generic words)."""
    stop = {"the", "of", "for", "and", "inc", "org", "organization", "foundation",
            "association", "society", "club", "chapter", "national", "american"}
    toks = [t for t in norm(org_name).split() if t not in stop and len(t) > 2]
    return toks or norm(org_name).split()


def text_mentions(text, tokens):
    t = norm(text)
    hits = sum(1 for tok in tokens if tok in t)
    return hits >= max(1, (len(tokens) + 1) // 2)  # at least half the distinctive tokens


def classify_org(name):
    for cat, rx in SENSITIVE.items():
        if rx.search(name or ""):
            return cat
    return "neutral"


# ----------------------------- stages -----------------------------

def stage_leads(st, max_leads, test):
    """Collect leads up to max_leads. leads_done means the SEARCH ran dry, not that a
    smaller earlier run (e.g. --test) finished — so a bigger --max-leads resumes."""
    if st["leads_done"] or len(st["leads"]) >= max_leads:
        print(f"[leads] cached: {len(st['leads'])}", file=sys.stderr)
        return
    seen_ids = {l["id"] or l["url"] for l in st["leads"]}
    page, token = st.get("next_page", 1), st.get("pg_token")
    while len(st["leads"]) < max_leads:
        resp = api_get("lead-search", {
            "currentJobTitles": ",".join(TITLES),
            "companyHeadcount": ",".join(HEADCOUNT),
            "locations": ",".join(LOCATIONS),
            "industryIds": ",".join(INDUSTRY_IDS),
            "page": page, "paginationToken": token})
        if not resp:
            break
        elements = resp.get("elements") or []
        if not elements:
            st["leads_done"] = True  # search ran dry — the true "done"
            break
        for el in elements:
            nm = first(el, "name", "fullName") or \
                 f"{el.get('firstName','')} {el.get('lastName','')}".strip()
            lid = first(el, "id", "profileId") or first(el, "linkedinUrl", "url")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            pos = next((p for p in el.get("currentPositions") or [] if p.get("title")), {})
            st["leads"].append({
                "name": nm,
                "title": pos.get("title") or first(el, "position", "title", "headline"),
                "company": pos.get("companyName") or first(el, "companyName", "company"),
                "companyUrl": pos.get("companyLinkedinUrl", ""),
                "url": first(el, "linkedinUrl", "profileUrl", "url"),
                "id": first(el, "id", "profileId", "publicIdentifier")})
        token = (resp.get("pagination") or {}).get("paginationToken")
        print(f"[leads] page {page}: {len(st['leads'])} collected", file=sys.stderr)
        page += 1
        st["next_page"], st["pg_token"] = page, token
        save_state(st)
        if test:
            break
    save_state(st)


def gate(lead):
    t = lead["title"] or ""
    if not PERSONA_RE.search(t):
        return "title: not comp/total-rewards"
    if EXCLUDE_TITLE_RE.search(t):
        return "title: excluded level"
    if not SENIOR_RE.search(t):
        return "title: below senior manager"
    return None


def company_is_tech(st, lead):
    """Verify the employer's industry via a cached company lookup. Fail-closed:
    unverifiable companies are cut (quality over volume) with the reason recorded."""
    curl = lead.get("companyUrl") or ""
    ck = norm(curl or lead["company"])
    if not ck:
        return False, "no company on lead"
    if ck not in st["companies"]:
        params = {"url": curl} if curl else {"search": lead["company"]}
        resp = api_get("company", params)
        el = (resp or {}).get("element") or resp or {}
        inds = el.get("industries") or []
        if isinstance(inds, str):
            inds = [inds]
        inds = [i.get("name", "") if isinstance(i, dict) else str(i) for i in inds]
        st["companies"][ck] = {"industries": [i for i in inds if i],
                               "name": first(el, "name")}
        save_state(st)
    inds = st["companies"][ck]["industries"]
    if not inds:
        return False, "company industry unverified"
    if any(TECH_INDUSTRY_RE.search(i) for i in inds):
        return True, ""
    return False, f"company not tech ({'; '.join(inds)[:60]})"


def stage_profiles(st, gated, cap):
    for lead in gated[:cap]:
        key = lead["url"] or lead["id"]
        if not key or key in st["profiles"]:
            continue
        resp = api_get("profile", {"url": lead["url"], "profileId": lead["id"] or None})
        el = (resp or {}).get("element") or resp or {}
        vols = []
        for v in el.get("volunteering") or []:
            vols.append({"org": first(v, "organizationName", "organization"),
                         "role": first(v, "role", "title"),
                         "start": first(v, "startDate"), "end": first(v, "endDate"),
                         "kind": "volunteer"})
        for o in el.get("organizations") or []:
            vols.append({"org": first(o, "name"),
                         "role": first(o, "positionHeld", "position"),
                         "start": first(o, "startDate"), "end": first(o, "endDate"),
                         "kind": "organization"})
        st["profiles"][key] = {"affiliations": vols,
                               "causes": el.get("causes") or [],
                               "publications": extract_pubs(el),
                               "fetched": bool(resp)}
        save_state(st)
        print(f"[profiles] {len(st['profiles'])} fetched", file=sys.stderr)


def extract_pubs(el):
    pubs = []
    for p in (el.get("publications") or [])[:3]:
        t = first(p, "title")
        link = first(p, "link", "url")
        if t:
            pubs.append(f"{t} ({link})" if link else t)
    return " | ".join(pubs)


def get_publications(st, key, lead):
    """Publications for a candidate; older cache entries predate the field, so
    lazily re-fetch the profile once for candidates only."""
    prof = st["profiles"].get(key) or {}
    if "publications" in prof:
        return prof["publications"]
    resp = api_get("profile", {"url": lead["url"], "profileId": lead["id"] or None})
    el = (resp or {}).get("element") or resp or {}
    prof["publications"] = extract_pubs(el)
    st["profiles"][key] = prof
    save_state(st)
    return prof["publications"]


def stage_activity(st, key, lead, org_name, org_page):
    """Contact-side evidence, STRICT:
    - authored: the post text contains the org's FULL name as an exact phrase
      (normalized) — never token-fraction matching.
    - engaged: counts ONLY if the engaged post was authored by the org's own
      LinkedIn page (the page's /company/... path appears in the item payload)."""
    ck = f"{key}||{norm(org_name)}"
    if ck in st["activity"]:
        return st["activity"][ck]
    ev = {"authored": 0, "latest": "", "links": [], "engaged_posts": []}
    phrase = norm(org_name)
    posts = api_get("profile-posts", {"profile": lead["url"], "profileId": lead["id"] or None})
    for p in (posts or {}).get("elements") or []:
        txt = norm(first(p, "content", "text", "commentary"))
        if phrase and phrase in txt:
            ev["authored"] += 1
            ev["latest"] = max(ev["latest"], first(p, "postedDate", "postedAt",
                                                   "date", "publishedAt"))
            link = first(p, "linkedinUrl", "url", "postUrl")
            if link:
                ev["links"].append(f"authored: {link}")
    page_path = ""
    if org_page and "linkedin.com" in org_page:
        page_path = org_page.rstrip("/").split("linkedin.com")[-1].lower()
    if page_path:
        for ep in ("profile-comments", "profile-reactions"):
            resp = api_get(ep, {"profile": lead["url"], "profileId": lead["id"] or None})
            for item in (resp or {}).get("elements") or []:
                if page_path not in json.dumps(item).lower():
                    continue  # engaged post not authored by the org's page
                post = item.get("post") or item
                link = first(post, "linkedinUrl", "url", "postUrl") or f"unlinked-{ep}"
                kind = "commented on org post" if ep == "profile-comments" \
                    else "reacted to org post"
                ev["engaged_posts"].append(link)
                if not link.startswith("unlinked"):
                    ev["links"].append(f"{kind}: {link}")
    st["activity"][ck] = ev
    save_state(st)
    return ev


def resolve_org(st, org_name, max_posts=5):
    """Org name -> its LinkedIn page, recent post URLs, page facts (size/about), and
    a donation-fit judgment. Cached across contacts."""
    ck = norm(org_name)
    cache = st["org_cache"].get(ck) or {}
    if "page" not in cache:
        cache = {"page": "", "posts": []}
        found = api_get("company-search", {"search": org_name})
        for c in (found or {}).get("elements") or []:
            if text_mentions(first(c, "name"), org_tokens(org_name)):
                cache["page"] = first(c, "linkedinUrl", "url")
                break
        if cache["page"]:
            posts = api_get("company-posts", {"company": cache["page"]})
            cache["posts"] = [first(p, "linkedinUrl", "url")
                              for p in (posts or {}).get("elements") or []][:max_posts]
    if "facts" not in cache:
        facts = {"staff": "", "about": "", "followers": ""}
        if cache["page"]:
            resp = api_get("company", {"url": cache["page"]})
            el = (resp or {}).get("element") or resp or {}
            facts["staff"] = str(el.get("employeeCount") or
                                 first(el, "employeeCountRange") or "")
            facts["about"] = (first(el, "description", "about", "tagline") or "")[:400]
            facts["followers"] = str(el.get("followerCount") or "")
        cache["facts"] = facts
    if "judge" not in cache:
        cache["judge"] = judge_org(org_name, cache["facts"])
    st["org_cache"][ck] = cache
    save_state(st)
    return cache


def judge_org(org_name, facts):
    """Donation-fit judgment: Haiku when ANTHROPIC_API_KEY is set, else a size
    heuristic from the org's LinkedIn staff count. Returns {size, about, fit, reason}."""
    j = {"size": "", "about": (facts.get("about") or "")[:80], "fit": "", "reason": ""}
    staff = re.sub(r"[^0-9]", "", (facts.get("staff") or "").split("-")[0]) or "0"
    n = int(staff)
    if n:
        j["size"] = ("local" if n <= 15 else "mid-size" if n <= 200 else "large")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        if j["size"] == "large":
            j["fit"], j["reason"] = "weak", "large org; donation impact diluted"
        elif j["size"]:
            j["fit"], j["reason"] = "moderate", "size heuristic only (no LLM judge)"
        return j
    body = {"model": ANTHROPIC_MODEL, "max_tokens": 150,
            "messages": [{"role": "user", "content":
                          "You judge whether a small donation made in a person's honor to this "
                          "organization would land as a thoughtful, personal gift. Charity-like, "
                          "community, or cause orgs = strong. Huge national charities = moderate "
                          "(real but impersonal). Trade/professional associations, alumni bodies, "
                          "clubs that mainly serve members = weak.\n"
                          f"Organization: {org_name}\n"
                          f"LinkedIn staff count: {facts.get('staff') or 'unknown'}; "
                          f"followers: {facts.get('followers') or 'unknown'}\n"
                          f"LinkedIn description: {facts.get('about') or 'none'}\n"
                          'Reply ONLY with JSON: {"size":"local|mid-size|large|unknown",'
                          '"about":"<what they do, max 8 words>",'
                          '"fit":"strong|moderate|weak","reason":"<max 12 words>"}'}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(),
                                 headers={"x-api-key": key,
                                          "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.loads(r.read().decode())["content"][0]["text"]
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            parsed = json.loads(m.group(0))
            for k in ("size", "about", "fit", "reason"):
                if parsed.get(k):
                    j[k] = str(parsed[k])[:100]
    except Exception as e:
        print(f"[judge] {org_name}: {type(e).__name__} — using heuristic", file=sys.stderr)
    return j


def org_side_engagement(st, cache, lead):
    """Org-side evidence: does the contact appear among reactors/commenters of the
    org's OWN recent posts? Returns (engaged post urls, evidence links)."""
    urls, links = [], []
    lead_name, lead_url = norm(lead["name"]), (lead["url"] or "").rstrip("/")
    for post_url in cache["posts"]:
        for ep in ("post-reactions", "post-comments"):
            resp = api_get(ep, {"url": post_url})
            for item in (resp or {}).get("elements") or []:
                blob = json.dumps(item)
                if (lead_url and lead_url in blob) or (lead_name and lead_name in norm(blob)):
                    urls.append(post_url)
                    kind = "reacted to org post" if ep == "post-reactions" else "commented on org post"
                    links.append(f"{kind}: {post_url}")
    return urls, links


def is_current(aff):
    end = (aff.get("end") or "").strip().lower()
    return end in ("", "present", "now")


def score(aff, ev, engagements):
    """Role-first ranking: a meaningful role (founder/board/officer/mentor) in a
    current affiliation is the primary Tier A signal; engagement/authored evidence
    is the secondary path. engagements = distinct org-authored posts engaged with."""
    if not is_current(aff):
        return "C", "affiliation ended"
    if LEADER_RE.search(aff.get("role") or ""):
        return "A", f"meaningful role: {aff.get('role')}"
    if engagements >= 2:
        return "A", f"engaged with {engagements} of the org's own posts"
    if ev["authored"]:
        return "A", f"posted about org by name ({ev['authored']}x" + \
               (f", latest {ev['latest']}" if ev["latest"] else "") + ")"
    if engagements == 1:
        return "B", "single engagement with an org post"
    return "B", "current listing, no activity evidence"


# ----------------------------- main -----------------------------

def main():
    ap = argparse.ArgumentParser(description="Volunteer-affiliation gift research (HarvestAPI)")
    ap.add_argument("--max-leads", type=int, default=500)
    ap.add_argument("--test", action="store_true", help="1 lead page, 5 profiles")
    ap.add_argument("--skip-org-side", action="store_true",
                    help="skip stage B (org-side engagement) to halve corroboration cost")
    a = ap.parse_args()

    st = load_state()
    stage_leads(st, a.max_leads, a.test)

    audit, gated = [], []
    batch = st["leads"][:a.max_leads]
    for lead in batch:
        reason = gate(lead)
        if not reason:
            ok, why = company_is_tech(st, lead)
            reason = why if not ok else None
        if reason:
            audit.append({**lead, "reason": reason})
        else:
            gated.append(lead)
    print(f"[gate] {len(gated)} of {len(batch)} pass persona/seniority + tech-industry",
          file=sys.stderr)

    stage_profiles(st, gated, cap=5 if a.test else len(gated))

    out = []
    for lead in gated:
        key = lead["url"] or lead["id"]
        prof = st["profiles"].get(key)
        if not prof:
            continue
        if not prof["fetched"]:
            audit.append({**lead, "reason": "profile fetch failed"})
            continue
        affs = [x for x in prof["affiliations"] if x["org"]]
        if not affs:
            audit.append({**lead, "reason": "no volunteer/org affiliation"})
            continue
        for aff in affs:
            if ORG_BLOCKLIST_RE.search(aff["org"]):
                audit.append({**lead, "reason": f"blocklisted org: {aff['org']}"})
                continue
            if not is_current(aff):
                audit.append({**lead, "reason": f"ended: {aff['org']}"})
                continue
            cat = classify_org(aff["org"])
            org = resolve_org(st, aff["org"])
            ev = stage_activity(st, key, lead, aff["org"], org["page"])
            org_urls, org_links = ([], []) if (a.skip_org_side or cat != "neutral" or
                                               not org["page"]) else \
                org_side_engagement(st, org, lead)
            # distinct org-authored posts the contact engaged with (both directions)
            engagements = len(set(ev["engaged_posts"]) | set(org_urls))
            tier, why = score(aff, ev, engagements)
            if tier == "C":
                audit.append({**lead, "reason": f"tier C: {why} ({aff['org']})"})
                continue
            if not org["page"]:
                why += "; org has no LinkedIn page (engagement unverifiable)"
            links = list(dict.fromkeys((ev.get("links") or []) + org_links))
            pubs = get_publications(st, key, lead)
            judge = org.get("judge") or {}
            giftable = ("REVIEW-SENSITIVE" if cat != "neutral" else
                        "REVIEW-FIT" if judge.get("fit") == "weak" else "YES")
            out.append({"Tier": tier, "Name": lead["name"], "Title": lead["title"],
                        "Company": lead["company"], "Profile URL": lead["url"],
                        "Organization": aff["org"], "Org Role": aff["role"],
                        "Org Type": aff["kind"], "Org Category": cat,
                        "Org Size": judge.get("size", ""),
                        "Org About": judge.get("about", ""),
                        "Donation Fit": (f"{judge.get('fit','')}"
                                         f"{' — ' + judge.get('reason','') if judge.get('reason') else ''}"),
                        "Giftable": giftable,
                        "Evidence": why,
                        "Evidence Links": " | ".join(links[:6]),
                        "Publications": pubs})

    os.makedirs(GIFT_DIR, exist_ok=True)
    day = date.today().isoformat()
    fit_rank = {"strong": 0, "moderate": 1, "": 2, "weak": 3}
    out.sort(key=lambda r: (r["Tier"],
                            fit_rank.get((r["Donation Fit"].split(" — ")[0]), 2),
                            r["Name"]))
    out_path = os.path.join(GIFT_DIR, f"gift_candidates_{day}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Tier", "Name", "Title", "Company",
                                          "Profile URL", "Organization", "Org Role",
                                          "Org Type", "Org Category", "Org Size",
                                          "Org About", "Donation Fit", "Giftable",
                                          "Evidence", "Evidence Links", "Publications"])
        w.writeheader(); w.writerows(out)
    audit_path = os.path.join(GIFT_DIR, f"gift_audit_{day}.csv")
    with open(audit_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "title", "company", "url", "id", "reason"],
                           extrasaction="ignore")
        w.writeheader(); w.writerows(audit)

    a_n = sum(1 for r in out if r["Tier"] == "A")
    print(f"gift_research: {len(out)} candidate rows (Tier A={a_n}, B={len(out)-a_n}) "
          f"-> {out_path}", file=sys.stderr)
    print(f"gift_research: {len(audit)} cut (see {audit_path})", file=sys.stderr)
    print(f"gift_research: {spend['requests']} requests ≈ ${spend['usd']:.2f} this run",
          file=sys.stderr)


if __name__ == "__main__":
    main()
