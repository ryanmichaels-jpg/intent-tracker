# Scrape spec (scrape machine)

The scrape runs on the **scrape** machine and drops **normalize-ready** raw CSVs into the
staging folder `comp-intel-raw` (transient — consumed and cleared by the ingest). It never
touches the CRM and never writes to the sheet.

Driver: [`scripts/scrape.py`](../scripts/scrape.py). Targets: `config/targets.json`
(gitignored; copy from `config/targets.example.json`). Secrets: `.env`.

## Two tracks

| Track | Apify actor | Produces |
|---|---|---|
| **Engagement** | `harvestapi/linkedin-company-posts` | People who reacted to / commented on competitor posts |
| **Jobs** | `harvestapi/linkedin-job-search` | Companies hiring target roles (budget signal) |

Both accept the same `APIFY_API_TOKEN`. The engagement actor takes **company page URLs and
exec profile URLs interchangeably** in one `targetUrls` array — per the PoC, named execs yield
far better cost-per-ICP than company pages, so configure both and lean on execs.

## Inputs (`config/targets.json`)

- `engagement.competitor_company_urls` / `competitor_exec_urls` — the targets.
- `engagement.competitor_label` — maps each target URL → a competitor name (fills the
  `Competitor` column).
- `engagement.drop_list` — URLs to **never** scrape (e.g. the known dead competitor page).
  These are removed from the target set at runtime.
- `engagement.posted_limit` / `posts_per_target` / `max_reactions_per_post` /
  `max_comments_per_post` — scope + cost caps.
- `jobs.titles` — a few **broad** seed queries (full role names). LinkedIn
  job search is fuzzy, so broad seeds catch title variants; precision is restored by the
  keyword filters below. Avoid bare, ambiguous seed tokens — a short word can match unrelated results.
- `jobs.locations`, `jobs.max_per_title`, `jobs.posted_limit`.
- `jobs.relevance_keywords` — positive filter: a result whose title contains none of these is
  dropped.
- `jobs.exclude_title_keywords` — negative filter: a result whose title contains any of these
  is dropped (used to remove off-target roles — a different domain than the
  target buyer).
- `jobs.exclude_competitor_companies` — when true, job postings from the competitor/own
  companies (the engagement `exclude_engager_companies` list) are dropped: a competitor hiring
  target roles is not a buying signal.

Keep this file out of git — it names real competitors/execs (employer-specific).

## Low-signal exclusions (engagement)

Dropped before a row is written:

- **Hiring / job-opening posts** (`exclude_hiring_posts`): a competitor announcing "we're
  hiring" draws applicants, not buyers — the whole post and its engagers are skipped
  (`HIRING_RE`).
- **Competitor / own-company employees** (`exclude_engager_companies`): an engager whose
  employer matches the list is dropped — this covers self-engagement (a competitor's employee on
  that competitor's post), cross-competitor insiders, and our own employees engaging with our
  exec's posts. Matched by **exact normalized company name** (so "compa" won't match
  "company"); checked on the headline-parsed company and again on the Apollo-enriched
  Current Company.
- **Company-page engagers**: LinkedIn company pages sometimes appear as reactors/commenters
  (e.g. "Competitor One · 97,700 followers"). They're not people/leads, so they're skipped (actor
  whose URL is a `/company/` page or whose position reads "N followers").
- **Dead pages** (`drop_list`): e.g. the inactive competitor page — its posts are never
  scraped (its founder's profile is targeted instead, and that competitor's employees are excluded via
  the list above).
- **ICP gate (positive match required).** An engager is **kept only if their title matches
  Tier 1** (your Tier-1 titles — `TIER1_RE`) **or Tier 2** (your Tier-2 titles
  — `TIER2_RE`), and isn't on the `EXCLUDE_ICP_RE` list. Flow: a cheap EXCLUDE drop runs on the
  noisy headline pre-enrichment (removes obvious junk like "Software Engineer"); then the
  **positive Tier-1/2 gate runs on the enriched real title** — so non-ICP titles like Physical
  Therapist, Customer Success, GTM, or even a Founder/CEO are dropped. Broaden `TIER1_RE`/
  `TIER2_RE` if a legitimate target title is being missed.

### Non-target-authored posts (reshares) → surface or review

A target's feed can surface a post **authored by someone else** (a reshare). For those:
- **Author clearly on-target** (post hits ≥2 target terms) → engagers are surfaced, with
  `Competitor = "<author> (discovered)"`.
- **Borderline** (1 target term) → the post is written to `config/review_candidates.json`
  (gitignored, human-review gate, same pattern as the bait watchlist) and its engagers are
  **not** surfaced until you approve the author into `watchlist.json`.
- **Not on-target** (0 terms) → dropped silently.

### Verified actor shapes (from the smoke test)

The engagement actor (`harvestapi/linkedin-company-posts`) returns **flattened** records — one
item per `post` / `reaction` / `comment` — with `actor` (the engager: `name`, `position`,
`linkedinUrl`), post text in `content`, comment text in `commentary`, and `commentIds` /
`reactionIds` on the post used to link engagers to their post (sidesteps the ugcPost/activity
twin-id mismatch). The jobs actor (`harvestapi/linkedin-job-search`) takes `jobTitles` (array)
+ `locations` (array); company is an object (`company.name`, `company.website`).

## Source-side dedupe (before the file is even written)

- **Engagement:** one row per **(engager, post)**. Within a single post, a person who both
  reacted and commented collapses to one row (**comment wins** — higher signal). Person
  identity = normalized **name + first 40 chars of headline**, *not* LinkedIn URL: reactions
  return the session-ID URL (`/in/ACoAA…`) while comments return the public handle
  (`/in/<username>`), so the same person otherwise looks like two people. Keeping rows
  separate **across** posts is intentional — that's the multi-touch signal the ingest turns
  into NEW vs REPEAT.
- **Jobs:** **one row per posting** (deduped by company+role+URL). Two genuinely-distinct
  postings of the same role → separate rows. (`# Postings` is no longer used — each row is a
  single posting; the Master column stays blank/`1` and is hidden in the rep view.)

The ingest (`normalize.py`) then does the cross-file/cross-week dedupe and the `ugcPost`↔
`activity` twin-URL collapse, so light double-coverage here is harmless.

## How the columns get filled

The scrape writes headers that `normalize.py`'s `ENGAGEMENT_MAP` / `JOB_MAP` already
recognize, so the drop is ingested with no extra mapping. The five newer columns:

| Column | Engagement | Jobs |
|---|---|---|
| **Title** | real job title from profile enrichment (`currentPosition[0].position`) — not the LinkedIn headline | the role title |
| **Company** | enriched current company (`currentPosition[0].companyName`) | hiring company |
| **Competitor** | the target whose post they engaged (the target's execs → `Competitor One`); `<author> (discovered)` for high-confidence non-target authors; blank otherwise | n/a |
| **Post Topic** | short themed label (keyword map → a short theme label; Haiku fills misses) | the job-signal term |
| **Post Type** | `reaction` / `comment` | n/a |
| **Hand Raiser** | `Y` when the engagement is a **comment on a bait post** (post text matches the bait markers); else `N` | n/a |
| **Email / Domain** | not enriched — left blank (Domain stays blank for engagement) | Domain parsed from company website |

## Current-company enrichment (engagement)

`enrich_current_company()` runs when `engagement.enrich_current_company` is true (default). It
collects each unique engager profile URL and bulk-scrapes them via
**harvestapi/linkedin-profile-scraper** in `'Profile details no email ($4 per 1k)'` mode —
reusing `APIFY_API_TOKEN` (no separate enrichment key/plan). It fills the real **Title**
(`currentPosition[0].position` — not the noisy headline) and **Company**
(`currentPosition[0].companyName`).

- **Email is intentionally not fetched** (the cheaper no-email mode), so the `Email` column
  stays blank. Switching to the `'+ email search ($10 per 1k)'` mode would populate it.
- **Domain** is not populated by this step (the profile scraper exposes the company's
  LinkedIn URL, not a web domain).
- Cost ≈ $4 per 1,000 unique engagers (one actor run per 100 URLs). Set
  `enrich_current_company: false` to skip.
- (We previously tried Apollo People Match — it requires a paid plan, so we use the Apify
  profile scraper instead.)

## Track 3 — bait discovery + hand-raiser surfacing

Driver: [`scripts/bait_discovery.py`](../scripts/bait_discovery.py) (actor
`harvestapi/linkedin-post-search`); config under `bait_discovery` in `config/targets.json`.
It searches target-term posts (with `scrapeComments`) and flags **bait posts** — defined as a
post containing **both** a comment/DM call-to-action **and** a promised deliverable
(`BAIT_CTA_RE` + `BAIT_DELIVERABLE_RE` in `scrape.py`), e.g. *"comment GUIDE and I'll send you
the template."* A standalone `👇` or the word "comment" is **not** bait. **Hiring posts are
excluded.** It then does **two** things:

1. **Surface hand-raisers now.** People who **comment on a bait post** are written immediately
   as engagement rows (`Source=Engagement`, `Hand Raiser=Y`, `Competitor="<author> (bait)"`)
   into `data/raw/bait_engagement_<date>.csv` — enriched + ICP-filtered like the main track.
   They reach reps in this week's batch; they are **not** held behind any threshold. (This is
   why Hand Raiser = comment-on-bait.)
2. **Grow the watchlist.** Bait-post **authors** (≥ `min_bait_posts`, above `min_engagement`)
   are proposed in `config/watchlist_candidates.json` (gitignored, **human-review gate** —
   they do **not** auto-join). Approve real target creators into `config/watchlist.json`; the
   engagement scraper then harvests their audience going forward
   (`engagement.include_approved_watchlist`).

**Two-pass for cost:** Pass 1 searches posts **without** comments (cheap, text only) and detects
bait on the text; Pass 2 (`harvestapi/linkedin-post-comments`) scrapes comments **only on the
few bait posts**. This avoids comment-scraping every searched post (the previous big spender).

**Reaching you:** `scripts/publish_review.py` writes both candidate lists to a **`Review` tab**
in Comp_Intel_Ready each run (via the service account — sheet structure only, never Master/rep
tabs), so headless runs surface candidates where you work. To act: review the tab, then add good
authors to `config/watchlist.json`.

Terms/thresholds are non-sensitive and live in committed config; populated watchlist / review
files (real names) are gitignored, with `*.example.json` templates committed.

## Remaining gaps / tuning

1. **`ANTHROPIC_API_KEY`** powers the Post Topic fallback for posts the keyword map misses;
   without it those topics fall back to a short snippet. Tune `THEME_MAP` over time.
2. **ICP gate** keeps only positive Tier-1/2 titles. If reps report missing a real target
   persona, broaden `TIER1_RE`/`TIER2_RE` (the gate fails closed — a missed match = a dropped
   lead).
3. **Non-target confidence** (≥2 target terms = surface, 1 = review) is a simple deterministic
   threshold — revisit once the review list has volume.

## Output

- Files: `{COMP_INTEL_RAW_DIR}/engagement_<date>.csv` and `jobs_<date>.csv`.
- `COMP_INTEL_RAW_DIR` should point at the **local** Drive-for-Desktop path of the
  `comp-intel-raw` folder so files sync to Drive (defaults to `./data/raw` if unset).

## Running it

```bash
python3 scripts/scrape.py --estimate-only   # offline cost estimate (no API calls)
python3 scripts/scrape.py --test            # small caps, LIVE — smoke test first!
python3 scripts/scrape.py                    # full run, both tracks
python3 scripts/scrape.py --track jobs       # one track
```

**Smoke test before trusting a full run.** The actors' exact output field names must be
confirmed against a real dataset — `scrape.py` parses defensively, but run `--test` once with
a real token + 1–2 targets and eyeball the CSVs before scheduling. Cost is ~$1–3/full run.

## Cadence

Weekly, ahead of the Sunday-night ingest (see [`scheduling.md`](scheduling.md)). The scheduled
runner sequences **scrape → ingest** on this machine so the files exist before ingest reads them.
