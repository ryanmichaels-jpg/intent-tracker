# Unified schema

One row per signal. Both scrape tracks share these columns (job-posting rows leave person-specific fields blank).

| Column | Filled by | Notes |
|---|---|---|
| Week | Ingest | ISO week, e.g. `2026-W23` |
| Source | Ingest | `Engagement` or `Job Posting` |
| Company | Ingest | engager's company OR hiring company |
| Domain | Ingest | for CRM domain-first match |
| Person Name | Ingest | engager; blank for jobs |
| Title | Ingest | engager title / job titles |
| Email | Ingest | enriched (~95% coverage) |
| Current Company | Ingest | enriched; may differ from engager company |
| Competitor | Ingest | which competitor's post; engagement only |
| Post Topic / Signal | Ingest | post topic or job-signal term |
| Post Type | Ingest | reaction / comment / bait-pattern |
| Hand Raiser | Ingest | `Y` if comment is a buying-intent trigger |
| # Postings | Ingest | jobs only |
| Post URL | Ingest | engagement only |
| NEW vs REPEAT | Ingest | vs prior weeks / multi-touch this week |
| **SFDC Account** | **Match step** | blank on handoff |
| **Assigned SDR** | **Match step** | blank; filled after CRM match |
| **Segment / Tier** | **Match step** | blank |
| Profile URL | Ingest | engager's LinkedIn profile URL; engagement only. Persisted so a row can be re-enriched (company/email) later without re-scraping |

Columns 16–18 (SFDC Account, Assigned SDR, Segment / Tier) are the handoff seam, filled by the match step. Column 19 (Profile URL) is carried by ingest for re-enrichment. Everything else is done by scrape + ingest.
