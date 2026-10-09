#!/usr/bin/env bash
# Chunk 5 F6 (2026-10-09): keep logs/*.log bounded on the e2-micro. A log over
# MAX_MB keeps its last KEEP_MB (the recent tail is what the ops sweep reads);
# *.drained digest archives older than 90 days are removed. Idempotent, quiet.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$HERE/logs"; MAX_MB="${ROTATE_MAX_MB:-20}"; KEEP_MB="${ROTATE_KEEP_MB:-5}"
[ -d "$LOGS" ] || exit 0
for f in "$LOGS"/*.log; do
  [ -f "$f" ] || continue
  size_mb=$(( $(stat -c %s "$f" 2>/dev/null || stat -f %z "$f") / 1048576 ))
  if [ "$size_mb" -ge "$MAX_MB" ]; then
    tail -c $((KEEP_MB * 1048576)) "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "rotate_logs: $(basename "$f") ${size_mb}MB -> last ${KEEP_MB}MB kept"
  fi
done
find "$LOGS" -name "*.drained" -mtime +90 -delete 2>/dev/null
exit 0
