# Intent miner (add-on)

Turns the comments we already scrape into **classified intent leads** with a defensible
evidence trail. Bolts onto the weekly run; the engagement/jobs tracks and the Master
append are untouched.

## What it is
The existing scrape harvests *who* engaged competitor/comp posts. The miner reads the
**comment text** of those (already ICP-filtered) commenters and judges intent:
`active_need` / `evaluating` / `curious` / `noise`, each backed by a **verbatim quote**
copied from the comment. The LLM only *proposes* bounded fields; deterministic code
*disposes* — a quote that isn't an exact substring of the comment can never surface,
no matter the model's confidence.

Ported from the Figma intent-miner engine; trimmed to this deployment:
- **No** Slack, **no** Salesforce/account-routing, **no** pipeline/monitoring/dashboard.
- **Keeps the existing scrape-side ICP filter** (`DEFAULT_ICP` in `scripts/scrape.py`) —
  the Figma `titles.py`/CPO-flip is **not** used.

## How it connects (no extra Apify cost)
`scripts/scrape.py` already receives each comment's text and post body from the
company-posts actor; it now writes them to a gitignored **comments dump**
(`data/raw/comments_<date>.json`) alongside the usual engagement CSV. The miner reads
that dump — no second actor call.

## Pipeline
```
comments dump → [post-type gate*] → intent classify* → verbatim gate → verify (praise downgrade) → richness → leads-<week>.csv
                 (* LLM stages — need ANTHROPIC_API_KEY)
```
- **post-type gate** (`intent/posttype.py`) — judges post_type + which Pave surface
  could displace the use case (`config/pave_capabilities.json`). Curated competitor
  sources are trusted, so a non-qualifying post **downgrades a surface to review**, it
  is never hard-dropped.
- **classify** (`intent/classify.py`) — `claude-haiku-4-5`, schema-constrained,
  conditioned on post type, self-consistency on borderline calls.
- **gate / verify / richness** (`intent/{gate,verify,richness}.py`) — deterministic.

## Two run modes (auto-selected by `ANTHROPIC_API_KEY`)
- **deterministic** (no key): every ICP-passed commenter becomes a `pending` lead with a
  richness score. Re-run with the key set to classify them. Today's CSV is unaffected.
- **live** (key set): full classify → gate → verify → richness → decision.

## Run
```bash
# 1) weekly scrape (also writes the comments dump):
python3 scripts/scrape.py
# 2) ingest as usual (unchanged):
python3 ingest/normalize.py data/raw/*.csv --out data/out/append-<week>.csv --week <week>
# 3) NEW — mine intent from the dump:
python3 -m intent.run_miner --week <week>      # reads latest data/raw/comments_*.json
```
Output: `data/out/leads-<week>.csv` (gitignored), sorted surface → review → pending →
drop, then by richness. Columns: decision, richness, intent, confidence, name, headline,
company, competitor, post_type, pave_surface, evidence_quote, suggested_angle, reason,
comment, profile_url, post_url.

## Tests
`python3 tests/test_intent.py` (or `pytest tests/test_intent.py`) — covers the verbatim
gate, praise verifier, richness, comp-tool matching, and a deterministic end-to-end run
over the synthetic fixture `intent/demo/demo_comments_dump.json`.

## To confirm / extend later
- `config/pave_capabilities.json` competitor sets are the transfer-brief drafts — confirm.
- Wide-net discovery (search-query + Google-boolean retrievers) was intentionally **not**
  ported; current discovery stays curated (`config/targets.json`).
