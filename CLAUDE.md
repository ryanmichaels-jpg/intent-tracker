# Working in this repo (Claude Code)

This is the **Competitive Intent Hub** — a weekly pipeline that scrapes competitive-intent
signals, normalizes/dedupes them, matches to CRM, and routes to the owning sales rep.

Read `HANDOFF.md` first for the current state and decisions. Then:

- The reusable engine is `ingest/normalize.py` (consolidate + dedupe + NEW/REPEAT). Schema in `docs/schema.md`.
- The weekly process is `docs/SOP.md`; scheduling options in `docs/scheduling.md`.
- The end-to-end diagram is `diagrams/pipeline-flow.html` (interactive; a `.png` render is embedded in the README).

## Hard rules
- **Never commit data.** No scraped records, real rep names, competitor names tied to an employer, or account emails. `data/` is gitignored; keep it that way.
- The ingest step has **no CRM access** by design. The CRM match/route is a separate, automated step that runs on a CRM-connected account (kept off the scrape machine so CRM access never lives next to the scrapers).
- Keep `ingest/schema.csv` and `docs/schema.md` in sync if columns change.

## Common tasks
- Run a batch: `python3 ingest/normalize.py data/raw/*.csv --out data/out/append.csv --week 2026-W23 --history data/master_export.csv`
- Add a new scraper field: extend `ENGAGEMENT_MAP`/`JOB_MAP` in `normalize.py`, add the column to `SCHEMA` and `schema.csv`.
- Schedule it: see `docs/scheduling.md` (`scripts/run_weekly_ingest.sh`).
