#!/usr/bin/env python3
"""
Append a normalized batch CSV to the 'Master' tab of the Comp_Intel_Ready sheet via the
Google Sheets API, authenticating as a service account (the work-owned sheet is shared to
that service account as Editor). This is the deterministic, no-LLM append step.

It NEVER touches CRM and NEVER fills the three handoff columns
(SFDC Account, Assigned SDR, Segment / Tier) — those stay blank on append and are
filled downstream by the separate CRM-connected step.

Config (env, typically loaded from .env):
  COMP_INTEL_SHEET_ID         spreadsheet ID of the canonical (work-owned) Comp_Intel_Ready
  COMP_INTEL_MASTER_TAB       tab name to append to (default: Master)
  GOOGLE_SERVICE_ACCOUNT_FILE path to the service-account JSON key (gitignored)

Usage:
    python3 ingest/upload_to_sheet.py data/out/append-2026-W23.csv [--dry-run]
"""
import argparse, csv, os, sys

# The 19-column unified schema, in order, using the canonical work-sheet labels. Columns 16-18
# are the CRM handoff columns, left blank on append (filled downstream by the CRM step); column
# 19 (Profile URL) is the engager's LinkedIn profile URL, carried so rows can be re-enriched
# later without re-scraping. The append is positional. We verify only the first 15 header labels
# against the live sheet before writing — those are the data columns that MUST align — and stay
# deliberately tolerant of the trailing labels so a cosmetic difference there never blocks an append.
SCHEMA = ["Week","Source","Company","Domain","Person Name","Title","Email",
          "Current Company","Competitor","Post Topic / Signal","Post Type","Hand Raiser",
          "# Postings","Post URL","NEW vs REPEAT","SFDC Account","Assigned SDR","Segment / Tier",
          "Profile URL"]
VERIFY_PREFIX = 15  # first N columns whose header labels must match exactly


def norm(s): return " ".join((s or "").strip().lower().split())


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        rows = list(r)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="normalized batch CSV produced by normalize.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate + report what would be appended, write nothing")
    a = ap.parse_args()

    sheet_id = os.environ.get("COMP_INTEL_SHEET_ID")
    tab = os.environ.get("COMP_INTEL_MASTER_TAB", "Master")
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    header, data_rows = load_csv(a.csv_path)
    if not header:
        print(f"{a.csv_path}: empty, nothing to append", file=sys.stderr)
        return
    # Local sanity: the batch CSV must itself be in schema order.
    if [norm(h) for h in header[:VERIFY_PREFIX]] != [norm(h) for h in SCHEMA[:VERIFY_PREFIX]]:
        sys.exit(f"ERROR: {a.csv_path} header is not in expected schema order; refusing to append.")
    if not data_rows:
        print(f"{a.csv_path}: header only, 0 data rows — nothing to append", file=sys.stderr)
        return

    if a.dry_run and (not sheet_id or not sa_file):
        # Allow validating the CSV offline before credentials / sheet ID exist.
        where = sheet_id or "<COMP_INTEL_SHEET_ID unset>"
        print(f"[dry-run] {len(data_rows)} rows from {a.csv_path} would be appended to "
              f"'{tab}' of sheet {where}. (Credentials/sheet ID not fully configured yet.)",
              file=sys.stderr)
        return

    if not sheet_id:
        sys.exit("ERROR: COMP_INTEL_SHEET_ID is not set (the canonical work-owned sheet ID).")

    if not sa_file or not os.path.exists(sa_file):
        sys.exit(f"ERROR: GOOGLE_SERVICE_ACCOUNT_FILE not found: {sa_file!r}")

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(
        sa_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values_api = svc.spreadsheets().values()

    # Verify the live tab's header so we never misalign columns.
    live = values_api.get(spreadsheetId=sheet_id, range=f"{tab}!1:1").execute().get("values", [[]])
    live_header = live[0] if live else []
    if [norm(h) for h in live_header[:VERIFY_PREFIX]] != [norm(h) for h in SCHEMA[:VERIFY_PREFIX]]:
        sys.exit(f"ERROR: live '{tab}' header does not match the expected schema "
                 f"(first {VERIFY_PREFIX} columns). Refusing to append.\n"
                 f"  live: {live_header[:VERIFY_PREFIX]}")

    if a.dry_run:
        print(f"[dry-run] verified live header OK; would append {len(data_rows)} rows to "
              f"'{tab}' of sheet {sheet_id}.", file=sys.stderr)
        return

    resp = values_api.append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:S",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": data_rows},
    ).execute()
    updated = resp.get("updates", {}).get("updatedRows", 0)
    print(f"appended {updated} rows to '{tab}' of {sheet_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
