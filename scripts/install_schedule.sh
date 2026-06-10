#!/usr/bin/env bash
# Install the weekly launchd job on THIS machine (Sunday 21:00 local).
# Idempotent: re-running re-installs cleanly. Does NOT touch the CRM or any secrets.
#
#   bash scripts/install_schedule.sh            # install / reinstall
#   bash scripts/install_schedule.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$(pwd -P)"
LABEL="com.comp-intel-hub.weekly"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "uninstalled $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__REPO_DIR__#${REPO_DIR}#g" scripts/com.comp-intel-hub.weekly.plist.example > "$PLIST_DEST"
echo "wrote $PLIST_DEST"

# Reload (bootout then bootstrap) so edits take effect.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
launchctl enable "$DOMAIN/$LABEL"
echo "loaded $LABEL — runs Sunday 21:00 local. Log: /tmp/comp-intel-weekly.log"
echo
echo "To actually WAKE the Mac if it's asleep at 21:00 (needs sudo, one-time):"
echo "  sudo pmset repeat wakeorpoweron S 20:55:00"
echo "Verify with:  pmset -g sched     |  launchctl print $DOMAIN/$LABEL | grep -i state"
echo "Trigger a manual run now:  launchctl kickstart -k $DOMAIN/$LABEL"
