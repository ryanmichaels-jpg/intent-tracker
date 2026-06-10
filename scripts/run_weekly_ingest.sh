#!/usr/bin/env bash
# Weekly competitive-intent pipeline (deterministic, no LLM). Runs on THIS scrape machine:
#   1. scrape   -> normalize-ready raw CSVs in the staging folder   (skipped if no APIFY token)
#   2. ingest   -> consolidate + dedupe + normalize + NEW/REPEAT -> data/out/append-<week>.csv
#   3. upload   -> append the batch to the Master tab via service account (never fills CRM cols)
#   4. archive  -> move consumed raw files out of staging (transient "consume and clear")
#
# Scrape runs FIRST and the script blocks until it finishes, so the files exist before ingest.
# Never touches the CRM. Wrap in caffeinate (see docs/scheduling.md) so the machine stays awake.
#
# Dry run (no API writes, keeps staging intact):  DRY_RUN=1 scripts/run_weekly_ingest.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env if present (APIFY_API_TOKEN, COMP_INTEL_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE, ...)
set -a; [ -f .env ] && . ./.env; set +a

# Prefer the project venv (has the Google client); fall back to system python3.
PYBIN="$(pwd)/.venv/bin/python3"; [ -x "$PYBIN" ] || PYBIN="python3"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::FutureWarning}"

WEEK=$(date +%G-W%V)
RAW_DIR="${COMP_INTEL_RAW_DIR:-data/raw}"
RAW_DIR="${RAW_DIR/#\~/$HOME}"
OUT="data/out/append-${WEEK}.csv"
DRY_RUN="${DRY_RUN:-0}"
HISTORY="data/master_export.csv"   # optional local export for cross-week REPEAT
mkdir -p data/out "$RAW_DIR"

echo "[$(date)] weekly ingest start (week=$WEEK, dry_run=$DRY_RUN, raw_dir=$RAW_DIR)"

# Idempotency: skip (before any Apify spend) if this ISO week was already ingested — e.g. an
# extra Sunday fire the same week. The marker is written only after a successful real append.
# Override with FORCE=1. (Dry runs never write the marker and never skip.)
MARKER="data/out/.ingested-${WEEK}"
if [ "$DRY_RUN" != "1" ] && [ "${FORCE:-0}" != "1" ] && [ -f "$MARKER" ]; then
  echo "[skip] week ${WEEK} already ingested ($MARKER exists). Set FORCE=1 to re-run."
  exit 0
fi

# 0) BAIT DISCOVERY (Track 3) -------------------------------------------------
# Refreshes config/watchlist_candidates.json for async human review. Candidates do NOT
# auto-feed this run — only the already-approved watchlist does. Non-fatal; skip with SKIP_BAIT=1.
if [ -n "${APIFY_API_TOKEN:-}" ] && [ "$DRY_RUN" != "1" ] && [ "${SKIP_BAIT:-0}" != "1" ]; then
  echo "[bait] discovering candidates"
  "$PYBIN" scripts/bait_discovery.py || echo "[bait] discovery failed (non-fatal)"
else
  echo "[bait] skipped (no token / dry run / SKIP_BAIT)"
fi

# 1) SCRAPE -------------------------------------------------------------------
if [ -n "${APIFY_API_TOKEN:-}" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "[scrape] dry run -> estimate only"
    "$PYBIN" scripts/scrape.py --estimate-only || echo "[scrape] estimate failed (non-fatal in dry run)"
  else
    echo "[scrape] running both tracks"
    "$PYBIN" scripts/scrape.py
  fi
else
  echo "[scrape] APIFY_API_TOKEN unset — skipping scrape, ingesting whatever is already staged"
fi

# 2) INGEST -------------------------------------------------------------------
shopt -s nullglob
RAW_FILES=( "$RAW_DIR"/*.csv "$RAW_DIR"/*.json )
if [ ${#RAW_FILES[@]} -eq 0 ]; then
  echo "[ingest] no raw files in $RAW_DIR — nothing to do. Done."
  exit 0
fi
echo "[ingest] normalizing ${#RAW_FILES[@]} file(s)"
HIST_ARG=()
[ -f "$HISTORY" ] && HIST_ARG=(--history "$HISTORY")
"$PYBIN" ingest/normalize.py "${RAW_FILES[@]}" --out "$OUT" --week "$WEEK" "${HIST_ARG[@]}"

# 3) UPLOAD -------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  echo "[upload] dry run"
  "$PYBIN" ingest/upload_to_sheet.py "$OUT" --dry-run
  echo "[done] dry run complete — staging left intact, nothing written to the sheet."
  exit 0
fi
echo "[upload] appending to Master"
"$PYBIN" ingest/upload_to_sheet.py "$OUT"
touch "$MARKER"   # mark this ISO week ingested (idempotency guard)

# 3.5) PUBLISH review candidates to the 'Review' tab (sheet structure only; non-fatal)
echo "[review] publishing bait/non-target candidates to the Review tab"
"$PYBIN" scripts/publish_review.py || echo "[review] publish failed (non-fatal)"

# 4) ARCHIVE (consume + clear staging) ---------------------------------------
ARCHIVE="data/raw/_consumed/${WEEK}"
mkdir -p "$ARCHIVE"
for f in "${RAW_FILES[@]}"; do mv "$f" "$ARCHIVE"/ 2>/dev/null || true; done
echo "[done] week=$WEEK appended and staging cleared (archived to $ARCHIVE)"
