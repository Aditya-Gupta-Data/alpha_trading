#!/usr/bin/env bash
# MANUAL OFFLINE TOOL — installs a git post-commit hook that syncs Architecture_Codex.md to Drive in the background.
# Usage: DRIVE_FOLDER_ID=<folder id> bash scripts/install_post_commit_hook.sh
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
HOOK="$(git -C "$REPO" rev-parse --git-path hooks)/post-commit"
MARK_BEGIN="# >>> drive-sync (managed by scripts/install_post_commit_hook.sh) >>>"
MARK_END="# <<< drive-sync <<<"

mkdir -p "$(dirname "$HOOK")"
[ -f "$HOOK" ] || printf '#!/usr/bin/env bash\n' > "$HOOK"

if grep -qF "$MARK_BEGIN" "$HOOK"; then
  echo "drive-sync block already present in $HOOK"
  exit 0
fi

cat >> "$HOOK" <<EOF

$MARK_BEGIN
if [ -n "\${DRIVE_FOLDER_ID:-}" ] || [ -f "\$HOME/.config/alpha_trading/drive_folder_id" ]; then
  (
    export DRIVE_FOLDER_ID="\${DRIVE_FOLDER_ID:-\$(cat "\$HOME/.config/alpha_trading/drive_folder_id" 2>/dev/null)}"
    cd "$REPO" && nohup python3 scripts/sync_to_drive.py >> "$REPO/logs/drive_sync.log" 2>&1 < /dev/null &
  ) >/dev/null 2>&1
fi
$MARK_END
EOF
chmod +x "$HOOK"

if [ -n "${DRIVE_FOLDER_ID:-}" ]; then
  mkdir -p "$HOME/.config/alpha_trading"
  printf '%s' "$DRIVE_FOLDER_ID" > "$HOME/.config/alpha_trading/drive_folder_id"
fi
mkdir -p "$REPO/logs"
echo "installed post-commit hook: $HOOK"
echo "folder id file: $HOME/.config/alpha_trading/drive_folder_id (write the Drive folder id there if not set above)"
