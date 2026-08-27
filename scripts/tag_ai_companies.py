#!/usr/bin/env python3
"""
Tag the AI-leadership master's companies on two axes:
  - Employer Type: employer | recruiter | unclear  (is the posting the real hiring
    company, or a search/staffing firm posting on behalf of a hidden client?)
  - Tech: yes | no | unclear                        (is the employer a tech company?)

Per unique company: one HarvestAPI company lookup (industry + description + staff),
cached. Then Haiku judges both axes in batches of 10 (cheap). Everything checkpoints
to data/out/company_tags.json so re-runs never re-pay.

Reuses HARVESTAPI_API_KEY + ANTHROPIC_API_KEY from .env.

Usage: python3 scripts/tag_ai_companies.py [master_csv]
"""
import csv, json, os, re, sys, time, urllib.error, urllib.request, urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(REPO, "data", "out", "ai-leadership-master_2026-08-27.csv")
CACHE = os.path.join(REPO, "data", "out", "company_tags.json")
HAIKU = "claude-haiku-4-5-20251001"
RECRUITER_RE = re.compile(r"\b(search|recruit|staffing|talent|executive search|headhunt|"
                          r"consult|advisor[sy]|partners|hr\b|human capital|placement|"
                          r"personnel|hiring|manpower)\b", re.I)


def api(endpoint, params, attempts=3):
    key = os.environ["HARVESTAPI_API_KEY"]
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    url = f"https://api.harvest-api.com/linkedin/{endpoint}?{qs}"
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"X-API-Key": key,
                "User-Agent": "Mozilla/5.0 (compatible; comp-intel-hub/1.0)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i < attempts - 1:
                time.sleep(3 * (i + 1)); continue
            print(f"[api] {endpoint} {type(e).__name__} — skip", file=sys.stderr)
            return None


def haiku_batch(items):
    """items: list of {name, industries, about}. Returns list of {employer_type, tech, note}."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return [{"employer_type": "unclear", "tech": "unclear", "note": "no LLM"} for _ in items]
    lines = []
    for i, it in enumerate(items):
        lines.append(f'{i}. {it["name"]} | industry: {it.get("industries","?")} | '
                     f'about: {(it.get("about") or "")[:160]}')
    prompt = ("For each organization decide two things:\n"
              '(a) employer_type: "employer" if it is the actual hiring company, '
              '"recruiter" if it is a staffing/executive-search/consulting firm that posts '
              'roles on behalf of client companies, "unclear" if unknown.\n'
              '(b) tech: "yes" if it is a technology/software company (incl. fintech, '
              'health-tech built on software), "no" if traditional (bank, hospital, '
              'manufacturer, retailer, university, government, agency), "unclear" if unknown.\n'
              "Organizations:\n" + "\n".join(lines) +
              '\nReply ONLY with a JSON array, one object per number in order: '
              '[{"employer_type":"...","tech":"...","note":"<=8 words"}]')
    body = {"model": HAIKU, "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}]}
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = json.loads(r.read().decode())["content"][0]["text"]
        arr = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
        out = []
        for i in range(len(items)):
            v = arr[i] if i < len(arr) else {}
            out.append({"employer_type": str(v.get("employer_type", "unclear"))[:12],
                        "tech": str(v.get("tech", "unclear"))[:8],
                        "note": str(v.get("note", ""))[:60]})
        return out
    except Exception as e:
        print(f"[haiku] batch {type(e).__name__} — fallback", file=sys.stderr)
        return [{"employer_type": "unclear", "tech": "unclear", "note": ""} for _ in items]


def main():
    rows = list(csv.DictReader(open(MASTER)))
    companies = sorted({r["Company"].strip() for r in rows if r["Company"].strip()},
                       key=str.lower)
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

    # 1) look up each company's facts (cached)
    for i, co in enumerate(companies):
        if co in cache and "facts" in cache[co]:
            continue
        resp = api("company", {"search": co})
        el = (resp or {}).get("element") or resp or {}
        inds = el.get("industries") or []
        if isinstance(inds, str): inds = [inds]
        inds = [x.get("name", "") if isinstance(x, dict) else str(x) for x in inds]
        cache[co] = {"facts": {"industries": "; ".join(i for i in inds if i),
                               "about": (el.get("description") or el.get("tagline") or "")[:200]}}
        if i % 20 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"[lookup] {i+1}/{len(companies)}", file=sys.stderr)
    json.dump(cache, open(CACHE, "w"))

    # 2) Haiku-judge in batches of 10 (cached by presence of 'tag')
    todo = [co for co in companies if "tag" not in cache[co]]
    for b in range(0, len(todo), 10):
        batch = todo[b:b+10]
        items = [{"name": co, **cache[co]["facts"]} for co in batch]
        for co, tag in zip(batch, haiku_batch(items)):
            cache[co]["tag"] = tag
        json.dump(cache, open(CACHE, "w"))
        print(f"[judge] {min(b+10,len(todo))}/{len(todo)}", file=sys.stderr)

    # 3) heuristic backstop for recruiter names Haiku missed, then write tagged master
    for co in companies:
        tag = cache[co]["tag"]
        if tag["employer_type"] == "unclear" and RECRUITER_RE.search(co):
            tag["employer_type"] = "recruiter"
    out = MASTER.replace(".csv", "_tagged.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Tier", "Company", "Employer Type", "Tech",
                                          "Job Titles", "Location", "Post URL", "Note"])
        w.writeheader()
        for r in rows:
            t = cache.get(r["Company"].strip(), {}).get("tag", {})
            w.writerow({**{k: r.get(k, "") for k in ("Tier", "Company", "Job Titles",
                        "Location", "Post URL")},
                        "Employer Type": t.get("employer_type", ""),
                        "Tech": t.get("tech", ""), "Note": t.get("note", "")})
    from collections import Counter
    tags = [cache[r["Company"].strip()]["tag"] for r in rows if r["Company"].strip() in cache]
    print("employer_type:", dict(Counter(t["employer_type"] for t in tags)), file=sys.stderr)
    print("tech:", dict(Counter(t["tech"] for t in tags)), file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
