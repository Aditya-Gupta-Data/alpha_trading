#!/usr/bin/env bash
# MANUAL OFFLINE TOOL — refreshes the NotebookLM Google Docs from origin/main whenever main has moved (Mac LaunchAgent).
# Reads a clean `git archive` of origin/main, never the working tree, so uncommitted edits never leave the Mac.
#   bash scripts/notebooklm_autosync.sh           # sync if origin/main changed since the last good run
#   bash scripts/notebooklm_autosync.sh --force   # sync now
set -u
REPO="${ALPHA_REPO:-$HOME/Documents/Claude/alpha_trading}"
STATE_DIR="$HOME/.config/alpha_trading"
LAST="$STATE_DIR/notebooklm_last_sha"
PY="${ALPHA_PYTHON:-python3}"
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

git -C "$REPO" fetch -q origin main || { echo "$(stamp) fetch failed; will retry next tick"; exit 0; }
SHA="$(git -C "$REPO" rev-parse origin/main)"
if [ "${1:-}" != "--force" ] && [ -f "$LAST" ] && [ "$(cat "$LAST")" = "$SHA" ]; then
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -C "$REPO" archive origin/main | tar -x -C "$TMP"
git -C "$REPO" log --name-only --format=@%ad --date=short origin/main \
    | awk '/^@/{d=substr($0,2);next} NF && !($0 in s){s[$0]=1; print $0 "\t" d}' > "$TMP/.file_dates.tsv"
export ALPHA_SYNC_SHA="$SHA"
echo "$(stamp) syncing origin/main ${SHA:0:7}"
if "$PY" "$TMP/scripts/sync_to_drive.py" --root "$TMP"; then
    mkdir -p "$STATE_DIR"
    printf '%s' "$SHA" > "$LAST"
    echo "$(stamp) done"
else
    echo "$(stamp) sync FAILED; will retry next tick"
fi
