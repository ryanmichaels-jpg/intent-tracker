# Running the weekly pipeline on a schedule (macOS, this laptop)

The whole pipeline is deterministic (no LLM), so it runs unattended via `launchd`. Both the
scrape and the ingest run on this one machine, sequenced **scrape → ingest → upload** inside
[`scripts/run_weekly_ingest.sh`](../scripts/run_weekly_ingest.sh) — the scrape blocks until it
finishes, so the raw files exist before the ingest reads them.

## What the runner does

1. **Scrape** (`scripts/scrape.py`) → normalize-ready CSVs in the staging folder. Skipped if
   `APIFY_API_TOKEN` is unset (then it just ingests whatever is already staged).
2. **Ingest** (`ingest/normalize.py`) → `data/out/append-<week>.csv` (dedupe + NEW/REPEAT).
3. **Upload** (`ingest/upload_to_sheet.py`) → append the batch to the `Master` tab via the
   service account. CRM columns stay blank.
4. **Archive** → move consumed raw files to `data/raw/_consumed/<week>/` (staging is transient).

Dry run (no writes, staging left intact):

```bash
DRY_RUN=1 bash scripts/run_weekly_ingest.sh
```

## Install the schedule (Sunday 21:00 local)

```bash
bash scripts/install_schedule.sh        # writes + loads ~/Library/LaunchAgents/com.comp-intel-hub.weekly.plist
```

The plist runs the runner under `caffeinate -i`, which keeps the Mac awake for the **duration
of the run** once it has started. Log: `/tmp/comp-intel-weekly.log`.

### Laptop-asleep case

`launchd`'s `StartCalendarInterval` does **not** wake a sleeping Mac on its own — it runs the
job when the Mac next wakes. To guarantee the Sunday-night run, schedule a wake a few minutes
before (one-time, needs sudo):

```bash
sudo pmset repeat wakeorpoweron U 20:55:00   # wake/power-on every Sunday 20:55 (pmset: U=Sun, S=Sat)
pmset -g sched                                # verify the wake schedule
```

Sequence on Sunday night: **20:55** Mac wakes (pmset) → **21:00** launchd fires the runner →
`caffeinate` holds it awake → scrape, ingest, upload, archive → done.

### Verify / operate

```bash
launchctl print gui/$(id -u)/com.comp-intel-hub.weekly | grep -i state   # loaded?
launchctl kickstart -k gui/$(id -u)/com.comp-intel-hub.weekly            # run now
tail -f /tmp/comp-intel-weekly.log                                        # watch a run
bash scripts/install_schedule.sh --uninstall                              # remove
```

## The CRM half (different machine)

The CRM match/route step runs on the separate CRM-connected account, **not** on this machine —
it needs CRM access, which is deliberately kept off the scrape laptop. It runs as its own
automated job over there (fuzzy-band name matches, 0.75–0.90, are flagged for human review
rather than auto-routed). Nothing in this repo ever writes to the CRM.
