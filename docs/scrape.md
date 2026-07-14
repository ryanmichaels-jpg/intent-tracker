# Scrape spec (scrape machine)

The scrape runs on the **scrape** machine and drops **normalize-ready** raw CSVs into the
staging folder `comp-intel-raw` (transient — consumed and cleared by the ingest). It never
touches the CRM and never writes to the sheet.

Driver: [`scripts/scrape.py`](../scripts/scrape.py). Targets: `config/targets.json`
(gitignored; copy from `config/targets.example.json`). Secrets: `.env`.

## Two tracks

Both run on **HarvestAPI's direct REST API** (`https://api.harvest-api.com`, auth via the
`X-API-Key` header, synchronous + paginated — no Apify actor runs, polling, or dataset
downloads). All calls share the same `HARVEST_API_KEY`.

| Track | HarvestAPI endpoints | Produces |
|---|---|---|
| **Engagement** | `/linkedin/company-posts` + `/linkedin/profile-posts` (posts), then `/linkedin/post-reactions` + `/linkedin/post-comments` (engagers) | People who reacted to / commented on competitor posts |
| **Jobs** | `/linkedin/job-search` | Companies hiring target roles (budget signal) |

The engagement track mixes **company page URLs and exec profile URLs** in the config target
list — per the PoC, named execs yield far better cost-per-ICP than company pages, so configure
both and lean on execs. At runtime each target is routed by URL type: a `/company/…` URL hits
`company-posts`, a profile URL hits `profile-posts`.

**Split-then-rank (the cost win).** The old Apify actor bundled posts + reactions + comments in
one run. The direct API splits them, so the scraper (a) fetches recent posts per target, (b)
ranks them locally by engagement (likes + comments + shares), and (c) pulls reactions/comments
**only for the top-N selected posts** — instead of scraping every engager on every post. Per-
endpoint call counts are logged at the end of the run. Knobs: `posts_scan_per_target` (ranking
pool, default `max(2·posts_per_target, 10)`) and `posts_per_target` (the top-N pulled for
engagers).

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
  "company"); checked on the headline-parsed company and again on the profile-enriched
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

### Verified API response shapes (confirm against a live smoke test)

Every list endpoint returns the standard envelope `{ elements: [...], pagination: { totalPages,
pageNumber, pageSize, paginationToken, ... }, status, query }`; `/linkedin/profile` returns a
single object under `element`. The scraper paginates by incrementing `page` and forwarding any
`paginationToken`, stopping at `totalPages` or an empty page.

- **Posts** (`company-posts` / `profile-posts`, param `company=` / `profile=`): each element has
  `id`, `content` (post text), `linkedinUrl`, `author` (`{name, publicIdentifier, universalName,
  linkedinUrl}`), and `engagement` (`{likes, comments, shares}`) — the local ranking key.
- **Reactions** (`post-reactions`, param `post=`): `{ reactionType, postId, actor: {name,
  position, linkedinUrl} }`. Note: `actor.linkedinUrl` comes back in profile-ID form
  (`/in/ACoAA…`), not the public slug.
- **Comments** (`post-comments`, param `post=`): `{ commentary (text), postId, actor: {name,
  position, linkedinUrl, author} }`; comment `actor.linkedinUrl` is the public-slug form.
- **Jobs** (`job-search`, params `search=` + `location=`): `{ title, url, postedDate, company:
  {name, universalName, linkedinUrl}, location: {linkedinText} }`. **There is no company web
  domain** in the job-search item (only the LinkedIn URL), so the `Domain` column stays blank for
  the jobs track and the CRM match/route step resolves it by company name.

Because reactions and comments are fetched **per selected post**, each engager's parent post is
known by construction — no `commentIds`/`reactionIds` linking is needed (that sidestepped the
old flattened-actor ugcPost/activity twin-id mismatch).

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
looks up each unique engager profile via the direct **`/linkedin/profile`** endpoint (reusing
`HARVEST_API_KEY`; no separate enrichment key/plan). It fills the real **Title**
(`currentPosition[0].position`, falling back to `experience[0].position` — not the noisy
headline) and **Company** (`currentPosition[0].companyName` / `experience[0].companyName`).

- **Email is intentionally not fetched** (the `findEmail` option is left off), so the `Email`
  column stays blank. Enabling it would populate the column at a higher per-profile rate.
- **Domain** is not populated by this step (the profile exposes the company's LinkedIn URL, not
  a web domain).
- The direct API does **not batch** profile lookups, so this is **one GET per unique engager**
  (results are cached by URL across rows). Set `enrich_current_company: false` to skip. The
  per-run profile-lookup count is logged alongside the engagement API call counts.

## Track 3 — bait discovery + hand-raiser surfacing

Driver: [`scripts/bait_discovery.py`](../scripts/bait_discovery.py) (endpoint
`/linkedin/post-search`); config under `bait_discovery` in `config/targets.json`.
It searches target-term posts (text only) and flags **bait posts** — defined as a
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

**Two-pass for cost:** Pass 1 searches posts via `/linkedin/post-search` (cheap, text only) and
detects bait on the text; Pass 2 fetches comments via `/linkedin/post-comments` **only on the
few bait posts** (one paginated query per post). This avoids comment-scraping every searched
post (the previous big spender).

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

For the standardized weekly procedure (frozen config, QA loop, scheduling, cost) see
**[`docs/RUNBOOK.md`](RUNBOOK.md)**. Commands:

```bash
python3 scripts/scrape.py --estimate-only   # offline cost estimate (no API calls)
python3 scripts/scrape.py --test --audit    # small caps, LIVE — smoke test + drop-audit first!
python3 scripts/scrape.py --audit            # full run, both tracks, with drop-audit
python3 scripts/scrape.py --track jobs       # one track
```

`--audit` additionally writes `data/raw/_audit/<track>_<date>.csv` — every dropped row + reason,
for checking the filters (false negatives). It's in a subdirectory the ingest never reads.

**Smoke test before trusting a full run.** The endpoints' exact response field names must be
confirmed against a real response — `scrape.py` parses defensively, but run `--test` once with a
real `HARVEST_API_KEY` + 1–2 targets and eyeball the CSVs before scheduling. Use
`--estimate-only` to project HarvestAPI credit spend first (per-result pricing, no Apify
platform margin — confirm the rate for your plan at harvestapi.io/pricing and override it via a
`pricing` block in `config/targets.json`).

## Cadence

Weekly, ahead of the Sunday-night ingest (see [`scheduling.md`](scheduling.md)). The scheduled
runner sequences **scrape → ingest** on this machine so the files exist before ingest reads them.
