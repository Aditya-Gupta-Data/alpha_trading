#!/bin/bash
# One-shot installer for the Procedural Evolution LaunchAgent (MAC ONLY).
# Copies the plist into ~/Library/LaunchAgents and registers it with
# launchd. Idempotent: re-running replaces any existing registration.
#
# NOT run automatically anywhere — execute it yourself when you decide to
# activate the Saturday 02:00 schedule:
#
#     bash scripts/install_evolution_agent.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.alphatrading.evolution"
SRC_PLIST="$REPO_ROOT/scripts/$LABEL.plist"
DEST_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI_TARGET="gui/$(id -u)"

if [ "$(uname)" != "Darwin" ]; then
    echo "[Evolution Agent] FATAL: this is a macOS LaunchAgent installer."
    echo "[Evolution Agent] The VM runs no Ollama (decision #47) — do not schedule evolution there."
    exit 1
fi

if [ ! -f "$SRC_PLIST" ]; then
    echo "[Evolution Agent] FATAL: $SRC_PLIST not found."
    exit 1
fi

plutil -lint "$SRC_PLIST" >/dev/null
echo "[Evolution Agent] plist syntax OK."

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"
# Replace any prior registration cleanly (bootout fails harmlessly if absent).
launchctl bootout "$GUI_TARGET/$LABEL" 2>/dev/null || true
cp "$SRC_PLIST" "$DEST_PLIST"
launchctl bootstrap "$GUI_TARGET" "$DEST_PLIST"
launchctl enable "$GUI_TARGET/$LABEL"

echo "[Evolution Agent] Installed: Saturdays 02:00 (Mac local time = IST here)."
echo "[Evolution Agent] Verify registration:  launchctl list | grep alphatrading"
echo "[Evolution Agent] Force a test run:     launchctl kickstart $GUI_TARGET/$LABEL"
echo "[Evolution Agent] Watch it:             tail -f \"$REPO_ROOT/logs/evolution.log\" \"$REPO_ROOT/logs/evolution.launchd.err\""
echo "[Evolution Agent] Reminder: /bin/bash needs Full Disk Access (already granted for the edge miner)."
