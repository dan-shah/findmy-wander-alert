#!/bin/bash
# Install (or reinstall) the monitor as a LaunchAgent for the current user.
set -euo pipefail

LABEL=com.dadtracker.monitor
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$PROJECT_DIR/.venv/bin/python" ] || { echo "No .venv in $PROJECT_DIR; see README step 2." >&2; exit 1; }
[ -f "$PROJECT_DIR/.env" ] || { echo "No .env in $PROJECT_DIR; see README step 3." >&2; exit 1; }

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"
sed "s#__PROJECT_DIR__#$PROJECT_DIR#g" "$PROJECT_DIR/launchd/$LABEL.plist" > "$DEST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed $DEST. Follow the log with: tail -f $PROJECT_DIR/logs/monitor.log"
