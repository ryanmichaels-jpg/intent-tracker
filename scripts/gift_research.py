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
                              r"consultant|recruiter|assistant\b)", re.I)

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
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"leads": [], "leads_done": False, "profiles": {}, "activity": {},
            "org_cache": {}}


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
    if st["leads_done"]:
        print(f"[leads] cached: {len(st['leads'])}", file=sys.stderr)
        return
    page, token = 1, None
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
            break
        for el in elements:
            nm = first(el, "name", "fullName") or \
                 f"{el.get('firstName','')} {el.get('lastName','')}".strip()
            pos = next((p for p in el.get("currentPositions") or [] if p.get("title")), {})
            st["leads"].append({
                "name": nm,
                "title": pos.get("title") or first(el, "position", "title", "headline"),
                "company": pos.get("companyName") or first(el, "companyName", "company"),
                "url": first(el, "linkedinUrl", "profileUrl", "url"),
                "id": first(el, "id", "profileId", "publicIdentifier")})
        token = (resp.get("pagination") or {}).get("paginationToken")
        print(f"[leads] page {page}: {len(st['leads'])} collected", file=sys.stderr)
        save_state(st)
        page += 1
        if test:
            break
    st["leads_done"] = True
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
                               "fetched": bool(resp)}
        save_state(st)
        print(f"[profiles] {len(st['profiles'])} fetched", file=sys.stderr)


def stage_activity(st, key, lead, tokens):
    """Contact-side evidence (stage A): authored posts + comments + reactions."""
    if key in st["activity"]:
        return st["activity"][key]
    ev = {"authored": 0, "engaged": 0, "latest": "", "links": []}
    posts = api_get("profile-posts", {"profile": lead["url"], "profileId": lead["id"] or None})
    for p in (posts or {}).get("elements") or []:
        txt = first(p, "content", "text", "commentary")
        if text_mentions(txt, tokens):
            ev["authored"] += 1
            ev["latest"] = max(ev["latest"], first(p, "postedDate", "date"))
            link = first(p, "linkedinUrl", "url", "postUrl")
            if link:
                ev["links"].append(f"authored: {link}")
    for ep in ("profile-comments", "profile-reactions"):
        resp = api_get(ep, {"profile": lead["url"], "profileId": lead["id"] or None})
        for item in (resp or {}).get("elements") or []:
            blob = json.dumps(item)[:2000]
            if text_mentions(blob, tokens):
                ev["engaged"] += 1
                post = item.get("post") or item
                link = first(post, "linkedinUrl", "url", "postUrl")
                if link:
                    kind = "commented" if ep == "profile-comments" else "reacted"
                    ev["links"].append(f"{kind}: {link}")
    st["activity"][key] = ev
    save_state(st)
    return ev


def org_side_engagement(st, org_name, lead, max_posts=5):
    """Org-side evidence (stage B): does the contact appear among reactors/commenters
    of the org's recent posts? Org page + posts are cached across contacts."""
    ck = norm(org_name)
    cache = st["org_cache"].get(ck)
    if cache is None:
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
        st["org_cache"][ck] = cache
        save_state(st)
    if not cache["page"]:
        return None, []  # org has no findable LinkedIn page — not evidence against
    hits, links = 0, []
    lead_name, lead_url = norm(lead["name"]), (lead["url"] or "").rstrip("/")
    for post_url in cache["posts"]:
        for ep in ("post-reactions", "post-comments"):
            resp = api_get(ep, {"url": post_url})
            for item in (resp or {}).get("elements") or []:
                blob = json.dumps(item)
                if (lead_url and lead_url in blob) or (lead_name and lead_name in norm(blob)):
                    hits += 1
                    kind = "reacted to org post" if ep == "post-reactions" else "commented on org post"
                    links.append(f"{kind}: {post_url}")
    return hits, links


def is_current(aff):
    end = (aff.get("end") or "").strip().lower()
    return end in ("", "present", "now")


def score(aff, ev, org_hits):
    """-> (tier, reason)"""
    if not is_current(aff):
        return "C", "affiliation ended"
    leader = re.search(r"(board|chair|president|founder|organizer|lead|director|trustee|"
                       r"treasurer|secretary|mentor)", aff.get("role") or "", re.I)
    if org_hits and org_hits >= 2:
        return "A", f"engages with org's posts ({org_hits} recent)"
    if ev["authored"]:
        return "A", f"posted about org ({ev['authored']}x, latest {ev['latest'] or 'n/a'})"
    if leader:
        return "A", f"leadership role: {aff.get('role')}"
    if ev["engaged"] or org_hits == 1:
        return "A", "engaged with org-related content"
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
    for lead in st["leads"]:
        reason = gate(lead)
        if reason:
            audit.append({**lead, "reason": reason})
        else:
            gated.append(lead)
    print(f"[gate] {len(gated)} of {len(st['leads'])} pass persona/seniority",
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
            if not is_current(aff):
                audit.append({**lead, "reason": f"ended: {aff['org']}"})
                continue
            cat = classify_org(aff["org"])
            tokens = org_tokens(aff["org"])
            ev = stage_activity(st, key, lead, tokens)
            org_hits, org_links = (None, []) if (a.skip_org_side or cat != "neutral") else \
                org_side_engagement(st, aff["org"], lead)
            tier, why = score(aff, ev, org_hits)
            if tier == "C":
                audit.append({**lead, "reason": f"tier C: {why} ({aff['org']})"})
                continue
            links = (ev.get("links") or []) + org_links
            out.append({"Tier": tier, "Name": lead["name"], "Title": lead["title"],
                        "Company": lead["company"], "Profile URL": lead["url"],
                        "Organization": aff["org"], "Org Role": aff["role"],
                        "Org Type": aff["kind"], "Org Category": cat,
                        "Giftable": "REVIEW-SENSITIVE" if cat != "neutral" else "YES",
                        "Evidence": why,
                        "Evidence Links": " | ".join(links[:6])})

    os.makedirs(GIFT_DIR, exist_ok=True)
    day = date.today().isoformat()
    out.sort(key=lambda r: (r["Tier"], r["Name"]))
    out_path = os.path.join(GIFT_DIR, f"gift_candidates_{day}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Tier", "Name", "Title", "Company",
                                          "Profile URL", "Organization", "Org Role",
                                          "Org Type", "Org Category", "Giftable",
                                          "Evidence", "Evidence Links"])
        w.writeheader(); w.writerows(out)
    audit_path = os.path.join(GIFT_DIR, f"gift_audit_{day}.csv")
    with open(audit_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "title", "company", "url", "id", "reason"])
        w.writeheader(); w.writerows(audit)

    a_n = sum(1 for r in out if r["Tier"] == "A")
    print(f"gift_research: {len(out)} candidate rows (Tier A={a_n}, B={len(out)-a_n}) "
          f"-> {out_path}", file=sys.stderr)
    print(f"gift_research: {len(audit)} cut (see {audit_path})", file=sys.stderr)
    print(f"gift_research: {spend['requests']} requests ≈ ${spend['usd']:.2f} this run",
          file=sys.stderr)


if __name__ == "__main__":
    main()
