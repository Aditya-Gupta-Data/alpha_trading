#!/usr/bin/env bash
# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code
# sweeps (CLAUDE.md Rule 5).
#
# The one command to run on the VM for a daily read:
#   1. the Stage-B 60-session clock + cron uptime canary
#   2. the Mac Handover Queue, as a literal copy-paste line
#
# READ-ONLY: it shells two stdlib-only Python readers. No DB connection is
# opened anywhere on this path — stage_b_tracker.py imports nothing from
# src/ at all, and the queue read is plain JSONL parsing. Nothing here
# writes, and nothing here runs the pipeline (a status tool that invoked
# the scorer could perturb the ledger it reports on, and heavy calls on the
# 1 GB e2-micro are an OOM risk).
#
# Usage:  bash scripts/daily_health_and_queue.sh
set -uo pipefail          # NOT -e: a failure in one half must still print
                          # the other half — this is a health tool.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
[ -x "$ROOT/venv/bin/python" ] && PY="$ROOT/venv/bin/python"

echo "============================================================"
echo " ALPHA TRADING — daily health  ($(date '+%Y-%m-%d %H:%M %Z'))"
echo "============================================================"
echo

"$PY" "$ROOT/scripts/stage_b_tracker.py" --root "$ROOT" \
  || echo "  (stage_b_tracker unavailable — check $ROOT/scripts)"

echo
echo "------------------------------------------------------------"
echo " MAC HANDOVER QUEUE"
echo "------------------------------------------------------------"

"$PY" - "$ROOT" <<'PYEOF'
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "data" / "mac_pending_tasks.jsonl"

HUMAN = {
    "rebuild_macro_artifacts": ("rebuild the macro templates/playbooks/"
                                "strategies artifacts on the Mac and ship "
                                "them to the VM"),
    "deep_history_backfill": "run the deep historical index/deal backfill on the Mac",
}

rows = []
if path.is_file():
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                pass

latest = {}
for r in rows:
    if r.get("task"):
        latest[r["task"]] = r
open_rows = [r for r in latest.values() if r.get("status") == "pending"]

if not open_rows:
    print("  Nothing pending — the VM has not declined any heavy work.")
else:
    print(f"  {len(open_rows)} task(s) the VM declined and handed to the Mac:")
    for r in open_rows:
        print(f"    - [{r['task']}] raised {r.get('raised_on')}")
        print(f"        why: {r.get('reason')}")
        if r.get("detail"):
            print(f"        {r['detail']}")
    names = ", ".join(HUMAN.get(r["task"], r["task"]) for r in open_rows)
    print()
    print("  Pending Mac Tasks: Copy and paste this to Claude on your Mac:")
    print()
    print(f"    Claude, please execute the following from the VM queue: {names}.")
print()
PYEOF
