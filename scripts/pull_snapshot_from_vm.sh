#!/bin/bash
# pull_snapshot_from_vm.sh — sync the engine's published market snapshot
# from the VM to this Mac, so the local dashboard can show live P&L
# WITHOUT the Mac ever fetching a quote itself (zero Dhan contention on the
# shared token, decision #48). Mirror image of scripts/push_token_to_vm.sh.
#
# The VM's live loop writes data/market_snapshot.json every ~60s during
# market hours (src/market_snapshot.py, wired into src/live_bridge). This
# copies that file down over `gcloud compute scp`; the local dashboard's
# GET /api/web/positions reads it and serves the engine's marks.
#
# Requires the snapshot-publishing code to be DEPLOYED on the VM first
# (built during the observation week, not yet deployed — see the ledger).
# Until then this simply copies whatever is (or isn't) there. Read-only on
# the VM: it pulls a file, changes nothing, restarts nothing.
#
#   bash scripts/pull_snapshot_from_vm.sh            # one pull
#   watch -n 60 bash scripts/pull_snapshot_from_vm.sh  # keep it fresh
#
# Run in a loop (or a LaunchAgent) alongside the dashboard if you want the
# Mac view to stay live through a session.

set -euo pipefail

VM="adigupta1998@alpha-trading-vm"
PROJECT="project-37632031-10d0-47dd-b6f"
ZONE="us-central1-a"
REMOTE="~/alpha_trading/data/market_snapshot.json"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)/data/market_snapshot.json"

echo "[pull-snapshot] $(date '+%H:%M:%S') copying engine snapshot from VM..."
if gcloud compute scp "${VM}:${REMOTE}" "${LOCAL}" \
        --project="${PROJECT}" --zone="${ZONE}" --quiet 2>/dev/null; then
    # Report freshness so a stale copy is obvious.
    python3 - "$LOCAL" <<'PY'
import json, sys, time
from datetime import datetime, timezone, timedelta
try:
    snap = json.load(open(sys.argv[1]))
    age = datetime.now(timezone(timedelta(hours=5, minutes=30))).timestamp() - float(snap["epoch"])
    print(f"[pull-snapshot] ok — published {snap['as_of']} ({age:.0f}s ago), "
          f"{len(snap.get('marks', []))} marks")
except Exception as e:
    print(f"[pull-snapshot] copied, but could not parse: {e}")
PY
else
    echo "[pull-snapshot] no snapshot on the VM yet (publishing code not "
    echo "                deployed?) — dashboard will fall back to a direct mark."
fi
