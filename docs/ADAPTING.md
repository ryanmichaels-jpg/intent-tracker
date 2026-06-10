# Adapting this pipeline to your own ICP

This repo is the **scrape + ingest engine** for a competitive-intent pipeline. It ships with
a placeholder ICP, but the targeting is data-driven — you can point it at a different
market (e.g. an SDR at a dev-tools company targeting engineering leaders) mostly through config.

## What you get vs. what you build

**In this repo (the top half of `diagrams/pipeline-flow.html`):**
- Two scrape tracks (LinkedIn engagement + job postings) and a self-growing bait-discovery loop, via Apify
- ICP gating, enrichment, hand-raiser flagging
- Consolidate + dedupe + NEW/REPEAT (`ingest/normalize.py`)
- Append to a Google Sheet `Master` tab (`ingest/upload_to_sheet.py`)
- A weekly runner + macOS scheduler

**NOT in this repo (you build it):** the CRM **match & route** half — matching each row to your
CRM, filling the handoff columns, and regenerating per-rep tabs. That runs on a CRM-connected
account by design (it needs CRM access and is kept off the scrape machine). The engine leaves the
last three columns blank for exactly this handoff.

## Prerequisites
- An [Apify](https://apify.com) account + API token (uses the `harvestapi` LinkedIn actors)
- A Google Cloud **service account** with the Sheets API enabled, and its JSON key
- A Google Sheet you own, shared to the service account as Editor (external-share must be allowed)
- Python 3.9+ and Drive-for-Desktop (or any folder sync) if you want the cross-machine drop

## Setup

```bash
git clone <your-fork> && cd intent-tracker
python3 -m venv .venv && .venv/bin/python3 -m pip install -r requirements.txt

cp config/targets.example.json config/targets.json   # gitignored — your real targets live here
cp .env.example .env                                  # fill in tokens + sheet id + SA key path
```

Then edit `config/targets.json`:
1. **`engagement`** — competitor company + exec LinkedIn URLs, `competitor_label`, `drop_list`, and `exclude_engager_companies` (competitors + your own company).
2. **`jobs`** — `titles` (use **full role names**, not bare words — LinkedIn job search is full-text, so `"Director of Operations"` matches every posting that mentions that phrase), `relevance_keywords`, `exclude_title_keywords`.
3. **`bait_discovery.target_terms`** — the topic terms whose engagement-bait posts you want to discover.
4. **`icp`** — retarget the persona classifier (below).

Create the Sheet's `Master` tab with the 18 columns in [`ingest/schema.csv`](../ingest/schema.csv) (the last three are the blank handoff columns), put its ID in `.env` as `COMP_INTEL_SHEET_ID`, and share it to the service account.

## Retargeting the ICP (the one part that's in code, made config-overridable)

The persona classifier lives in [`scripts/scrape.py`](../scripts/scrape.py) as `DEFAULT_ICP`. You don't
edit the code — you add an optional **`icp` block to `config/targets.json`** that overrides any of
its keys. Anything you omit keeps the built-in default, so a partial block is fine.

| Key | Meaning | Format |
|---|---|---|
| `tier1` | Primary personas to **keep** | regex string (case-insensitive) |
| `tier2` | Secondary personas to **keep** | regex string |
| `exclude` | Titles to **drop** (a tier1/tier2 hit wins over an incidental exclude word) | regex string |
| `themes` | Post-topic labels (first match wins) | list of `[regex, label]` pairs |
| `hiring` | Posts to skip entirely (low signal) | regex string |

An engager is kept only if their enriched title matches `tier1` or `tier2` and isn't dropped by `exclude`.

### Example: a dev-tools SDR (e.g. targeting engineering leaders)

```json
"icp": {
  "tier1": "(software\\s*engineer|developer|developer\\s*experience|platform\\s*eng\\w*|infrastructure|devtools|sre|site\\s*reliability)",
  "tier2": "(engineering\\s*manager|staff\\s*engineer|principal\\s*engineer|head\\s*of\\s*eng\\w*|\\bvp\\s*eng\\w*|\\bcto\\b)",
  "exclude": "\\b(recruiter|sourcer|sales|account\\s*executive|\\bsdr\\b|\\bbdr\\b|marketing|customer\\s*success|designer|product\\s*manager)\\b",
  "themes": [
    ["ci\\s*/?\\s*cd|build\\s*pipeline|deploy", "CI/CD"],
    ["kubernetes|k8s|container", "Kubernetes"],
    ["developer\\s*experience|\\bdx\\b|internal\\s*platform", "Developer Experience"],
    ["incident|on-?call|observability", "Reliability"]
  ],
  "hiring": "(\\bwe'?re hiring\\b|\\bnow hiring\\b|\\bopen role[s]?\\b|\\bjoin our team\\b|\\bapply now\\b|#hiring)"
}
```

(JSON requires escaped backslashes: write `\\s`, `\\b`, `\\w` for the regex `\s`, `\b`, `\w`.)

### Verify your ICP before a live run (free — no Apify spend)

```bash
.venv/bin/python3 - <<'PY'
import sys; sys.path.insert(0, "scripts"); import scrape
for t in ["Staff Software Engineer", "VP Engineering", "Recruiter", "Head of Engineering"]:
    print(f"{t!r:40} keep={scrape.is_icp(t)}  drop_pre_enrich={scrape.is_non_icp(t)}")
PY
```

Tune the regexes until the keep/drop verdicts match your market.

## Smoke-test the wiring (cheap)

```bash
set -a; . ./.env; set +a
.venv/bin/python3 scripts/scrape.py --estimate-only        # offline cost estimate, no API
.venv/bin/python3 scripts/scrape.py --test --track jobs    # tiny live jobs pull
.venv/bin/python3 scripts/scrape.py --test --track engagement
.venv/bin/python3 ingest/upload_to_sheet.py data/out/append-*.csv --dry-run   # verifies live header, writes nothing
```

## Schedule it
- **macOS:** `bash scripts/install_schedule.sh` (Sunday 21:00; see [scheduling.md](scheduling.md)).
- **Linux/other:** wrap `scripts/run_weekly_ingest.sh` in a cron entry.

## Reminders
- Never commit `config/targets.json`, `.env`, the SA key, or any `data/` — all gitignored. Keep it that way.
- The engine never touches the CRM; the match/route half is yours to build.
