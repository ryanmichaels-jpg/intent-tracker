# Weekly run SOP

## Three roles
- **Scrape** (scrape machine): run the engagement + job-posting scrapes; raw CSV/JSON land in the synced `comp-intel-raw` drive folder.
- **Ingest** (automation account, no CRM): consolidate → dedupe → normalize → tag NEW vs REPEAT → append a dated batch to the `Master` tab.
- **Match & route** (CRM-connected account): an automated job that matches Company/Domain → fills the three handoff columns → regenerates per-rep tabs → posts the summary (fuzzy-band name matches go to human review).

## Weekly flow
1. **Scrape.** Run both tracks. Raw files land in `comp-intel-raw`.
2. **Ingest.** `python3 ingest/normalize.py data/raw/*.csv --out data/out/append.csv --week <ISO> --history data/master_export.csv`, then append `append.csv` to the `Master` tab.
3. **Match & route.** CRM match by Domain (fuzzy name 0.75-0.90 = human review, not auto-route) → fill `SFDC Account / Assigned SDR / Segment-Tier` → regenerate rep tabs.

## Sheet structure
- `Master` — full source of truth + weekly append target.
- One **materialized tab per rep** — filtered copy, regenerated each week.
- `Gradient Ready` — claimable / no rep assigned.
- `Unassigned` — no CRM match yet.

## Match & route notes (work / CRM account only)

The Master tab keeps all 18 columns (the ingest never changes that). The match step on the
work account:

- **`Assigned SDR` ← SFDC `Assigned SDR` field**, *not* `Account Owner` (SDRs aren't listed as
  account owners). Match Company/Domain → read SFDC's `Assigned SDR` → write it into the
  `Assigned SDR` column (1:1, same name).
- **`SFDC Account`** = the resolved SFDC Account (name/link) for the row's Company/Domain.
- **Keep the rep view uncluttered.** Master stays full, but each materialized rep tab should
  show only this subset, in order — hide the rest:

  `Source · Name · Title · Company · Competitor · Post Topic · Post URL · Segment/Tier`

  (`Source` shown collapsed, e.g. "Engagement – comment (hand raiser)"; hide Week, Domain,
  Email, Post Type, Hand Raiser, # Postings, NEW vs REPEAT, SFDC Account from reps.)

## Dedupe & NEW/REPEAT rules
- Collapse LinkedIn twin URLs (same person+company+topic, URL differs only by `ugcPost` vs `activity` — keep `activity`); drop exact dupes.
- REPEAT if same person+company seen in a prior week OR engaged with >=2 distinct posts this batch; else NEW.

## Notes
- Genericize rep names, competitor names, and account emails before publishing this repo.
- Nothing is written back to the CRM; this is read + route only.
