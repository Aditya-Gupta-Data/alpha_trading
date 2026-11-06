#!/usr/bin/env bash
# MANUAL OFFLINE TOOL — installs the Mac LaunchAgent that keeps the NotebookLM Google Docs current (hourly + at login).
#   bash scripts/install_notebooklm_autosync.sh              # install / reinstall
#   bash scripts/install_notebooklm_autosync.sh --uninstall
set -euo pipefail
LABEL="com.alphatrading.notebooklm-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/.config/alpha_trading"
RUNNER="$STATE_DIR/notebooklm_autosync.sh"
LOG="$HOME/Library/Logs/alpha_trading_notebooklm.log"
REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
REPO="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir | sed 's#/.git$##')"
PY="$(command -v python3)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$PLIST" "$RUNNER"
    echo "uninstalled $LABEL"
    exit 0
fi

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
cp "$(dirname "$0")/notebooklm_autosync.sh" "$RUNNER"
chmod +x "$RUNNER"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$RUNNER</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ALPHA_REPO</key><string>$REPO</string>
        <key>ALPHA_PYTHON</key><string>$PY</string>
        <key>PATH</key><string>$(dirname "$PY"):/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>3600</integer>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
echo "installed $LABEL: repo $REPO, python $PY, log $LOG"
