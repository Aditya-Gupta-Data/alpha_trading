#!/bin/bash
# publish_dashboard_mirror.sh — runs ON THE TRADING VM (cron #33, decision #112):
# push the five ledger files the showcase reads to the always-on dashboard box
# over ssh/rsync (a dedicated key, ~/.ssh/dashboard_push, that can only log in
# as the dashboard user). Read-only on the engine's files; the box never
# connects back. Target set in ~/.dashboard_target (user@host:/path), one line.
#   bash scripts/publish_dashboard_mirror.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${DASHBOARD_RSYNC_TARGET:-$(cat "$HOME/.dashboard_target" 2>/dev/null || true)}"
KEY="${DASHBOARD_PUSH_KEY:-$HOME/.ssh/dashboard_push}"
if [ -z "$TARGET" ]; then echo "[publish] no target configured (~/.dashboard_target) — nothing pushed"; exit 0; fi
files=()
for f in data/brain_map.db data/journal.jsonl logs/equity_shadow_journal.jsonl data/market_snapshot.json logs/recon.jsonl; do
  [ -f "$HERE/$f" ] && files+=("$HERE/$f") || echo "[publish] absent $f"
done
# --temp-dir + delay-updates: the box never reads a half-written file
if rsync -az --timeout=60 --delay-updates -e "ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new" "${files[@]}" "$TARGET/" 2>&1; then
  echo "[publish] $(date '+%F %T') ${#files[@]} file(s) -> $TARGET"
else
  echo "[publish] $(date '+%F %T') PUSH FAILED -> $TARGET"
fi
