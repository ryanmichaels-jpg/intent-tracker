# Project handoff / state

## What this is
A weekly competitive-intent pipeline. Two scrape tracks — LinkedIn **engagement** (people
interacting with competitor / category posts) and target-role **job postings**
(hiring intent) — are consolidated into one sheet, deduped, normalized, CRM-matched, and
routed to the owning rep. Each rep sees only their accounts.

## Three-machine model
1. **Scrape** (scrape machine): LinkedIn engagement + job-posting scrapes -> raw CSV/JSON into synced `comp-intel-raw` drive folder.
2. **Ingest** (automation account, no CRM): consolidate + dedupe + normalize + NEW/REPEAT -> append a dated batch to the `Master` tab. This is what `ingest/normalize.py` does.
3. **Match & route** (CRM-connected account): match Company/Domain -> fill the 3 handoff columns -> regenerate per-rep tabs. Runs as a separate automated job on the CRM-connected account (kept off the scrape machine so CRM access stays isolated).

## Sheet: Comp_Intel_Ready
- `Master` (source of truth + append target), one materialized tab per rep, `Gradient Ready` (claimable), `Unassigned` (no CRM match).
- Schema: see `docs/schema.md`. Handoff seam = the last three columns (SFDC Account, Assigned SDR, Segment/Tier), filled by the match step.

## Status
- DONE: schema locked; `normalize.py` engine; dedupe (collapse ugcPost/activity twin URLs); NEW vs REPEAT; per-rep breakout structure; SOP; sanitized repo.
- A backfill of prior engagement data was migrated (twin-URL dupes collapsed) into the Master/per-rep structure. That data is NOT in this repo (privacy).
- LEFT: ship the new scraper columns (Domain, Email, Current Company, Post Type, Hand Raiser); wire the drive-folder read in the scheduled runner; add the job-posting track's data; CRM match/route automation on the CRM account.

## Known gaps
- Migrated backfill lacked Domain/Email/Post Type/Hand Raiser (not in old data) and original week labels (-> `backfill`).
- Job-posting rows not yet present in the consolidated sheet.
- A dead competitor page exists in the drop list — keep on drop list, don't scrape.
- Fuzzy CRM name match 0.75-0.90 = human review, not auto-route. Nothing is written back to the CRM.

## Before publishing
Genericize any rep names, employer-specific competitor names, and account emails. Confirm `data/` has nothing committed.
