# Weekly run RUNBOOK (standardized)

The operating standard for the weekly competitive-intent scrape on **HarvestAPI's direct API**.
Follow this to reproduce today's tuned results every week. Companion docs: `docs/scrape.md`
(how the scrape works), `docs/SOP.md` (ingest + sheet), `docs/scheduling.md` (scheduler),
`docs/ADAPTING.md` (retargeting to a different ICP).

## What "same results every week" means

The **config, filters, and ICP are frozen and version-controlled** — so the *methodology* is
identical every run. The **data is fresh each week by design**: LinkedIn returns whichever
people engaged / whichever roles were posted in the last 7 days, so the *rows* differ week to
week (that's the signal). Reproducible = same targets, same ranking, same ICP gate, same
dedupe, same output schema — not identical rows.

## One-time setup (per machine)

1. **Python + repo.** Clone the repo; `python3` (3.9+) is all the scrape needs (stdlib only).
2. **Secrets.** `cp .env.example .env`, then set `HARVEST_API_KEY` (from harvestapi.io).
   Optional: `ANTHROPIC_API_KEY` (Post-Topic labels for posts the keyword map misses).
3. **Targets.** `cp config/targets.example.json config/targets.json`, then fill the **real**
   competitor company/exec URLs and their `competitor_label` mapping. Everything else in the
   file (ICP regexes, job titles, relevance/bait keywords, caps) is the **validated config —
   leave it as-is** to get the standard results. `config/targets.json` is gitignored (it names
   real competitors); the reproducible filters live in the committed `targets.example.json`.
4. **Confirm your credit rate.** Check your plan's per-result cost at harvestapi.io/pricing; if
   it isn't ~$0.004/result, set a `pricing` block in `config/targets.json` so `--estimate-only`
   is accurate.

## The weekly command

```bash
python3 scripts/scrape.py --audit          # both tracks, full caps, with drop-audit
```

Writes, into `data/raw/` (or `COMP_INTEL_RAW_DIR` if set):
- `engagement_<date>.csv`, `jobs_<date>.csv` — normalize-ready outputs (schema in `docs/schema.md`)
- `_audit/engagement_<date>.csv`, `_audit/jobs_<date>.csv` — every dropped row + reason (QA)

To take it all the way into the Google Sheet (needs `COMP_INTEL_SHEET_ID` +
`GOOGLE_SERVICE_ACCOUNT_FILE` in `.env`): `bash scripts/run_weekly_ingest.sh` runs
scrape → normalize (dedupe + NEW/REPEAT) → append to the `Master` tab.

## Schedule it (Sunday 21:00, macOS launchd)

```bash
bash scripts/install_schedule.sh                       # install / reinstall
sudo pmset repeat wakeorpoweron S 20:55:00             # optional: wake the Mac to run
```

This installs `~/Library/LaunchAgents/com.comp-intel-hub.weekly.plist`, which runs
`scripts/run_weekly_ingest.sh` under `caffeinate` every Sunday at 21:00 (log:
`/tmp/comp-intel-weekly.log`). The runner **skips the scrape if `HARVEST_API_KEY` is unset**.
Uninstall: `bash scripts/install_schedule.sh --uninstall`. Manual fire:
`launchctl kickstart -k gui/$(id -u)/com.comp-intel-hub.weekly`.

If you only want the raw CSVs (no sheet upload), point the schedule at
`python3 scripts/scrape.py --audit` instead of the full runner.

## The ICP filter (the standardized "keep" logic)

Defined in `config/targets.json` → `icp` (regex, case-insensitive). An engager is **kept only
if** their enriched title matches **Tier-1 or Tier-2** and isn't a company page / competitor
employee / hiring post. A tier match **wins over** the exclude list (so a real exec with a
stray word survives).

- **Tier 1 (decision-makers):** Chief People Officer / CHRO; VP / SVP / Head / Director of
  **Total Rewards, Compensation, People, or HR** — matched in either word order (so both
  "Director, Total Rewards" and "HR Director" hit).
- **Tier 2 (influencers):** individual contributors + managers/analysts in **rewards /
  compensation / comp & benefits / people ops / HRBP / benefits**.
- **Exclude (pre-enrichment drop):** brand / PR / comms / marketing / customer success /
  sales / AE / recruiter / student / intern / software / engineer / designer.

Also dropped, by design: **hiring posts** (competitor "we're hiring" draws applicants, not
buyers), **competitor / own-company employees** (`exclude_engager_companies`, exact-name match),
and **company-page reactors**. Jobs `Domain` is intentionally blank (the job-search API returns
no company website; the CRM match step resolves by company name).

## QA loop — run before trusting any change

Never trust a config change blind. Smoke-test with small caps + the audit trail:

```bash
python3 scripts/scrape.py --test --audit
```

Then inspect what was kept vs dropped:

```bash
python3 - <<'EOF'
import csv, glob
from collections import Counter
def latest(p):
    g = sorted(glob.glob(p)); return g[-1] if g else None
ea, ek = latest('data/raw/_audit/engagement_*.csv'), latest('data/raw/engagement_*.csv')
if ea:
    rows = list(csv.DictReader(open(ea)))
    print('DROP REASONS:', dict(Counter(r['Reason'] for r in rows)))
    print('\n--- cut as non-ICP (real title | company) — scan for real buyers wrongly dropped ---')
    for r in rows:
        if r['Reason'].startswith('non-ICP'): print('  ', r['Title/Headline'], '|', r['Company'])
print('\n--- KEPT (title | company | competitor) — scan for junk that survived ---')
if ek:
    for r in csv.DictReader(open(ek)): print('  ', r['Title'], '|', r['Current Company'], '|', r['Competitor'])
EOF
```

- A real comp/HR buyer in the **cut** list → widen a Tier regex.
- Junk in the **KEPT** list → add an exclude term.
- Then re-run `--test --audit` until both look right, and commit the config change.

## Cost

HarvestAPI is pay-as-you-go, billed **per returned result** (no Apify platform margin).
Preview before spending:

```bash
python3 scripts/scrape.py --estimate-only      # offline upper-bound projection
```

The run logs **per-endpoint call counts** so you can see where credits go (post fetches rank
cheaply; reactions/comments/profile lookups are the spend). Enrichment (`/linkedin/profile`,
one lookup per unique engager) is ~half the engagement cost — set
`engagement.enrich_current_company: false` to skip it (you then lose real Title/Company and fall
back to the LinkedIn headline).

## Change control

To change what gets scraped or kept, edit `config/targets.json` **only** (never hard-code
filters in `scrape.py`). Then: run the QA loop above → confirm → mirror any non-sensitive change
(ICP, titles, keywords, caps — not competitor URLs) into the committed
`config/targets.example.json` so the standard stays version-controlled and reviewable.

## Validation record (transport swap → HarvestAPI direct)

Confirmed live before first full run:
- Transport swap works end-to-end (browser User-Agent required to clear HarvestAPI's Cloudflare
  filter; auth via `X-API-Key`).
- Output fields match the prior (Apify-era) run column-for-column: Person Name, Title,
  Competitor, Post Topic, Post Type, Post URL, Profile URL; `Domain` blank for engagement/jobs.
- Enrichment fixed to look up reaction engagers by **profileId** (their `/in/ACoAA…` session
  URLs don't resolve by URL) — fill rate went from ~6% to ~100%.
- ICP tuned to comp/total-rewards and verified in both directions via `--audit` (real buyers
  kept, non-buyers dropped).
