#!/bin/bash
# pull_dashboard_data.sh — mirror the VM's live ledgers to data/vm_mirror/ so
# the Streamlit showcase (src/dashboard/app.py) shows the REAL desk, not this
# Mac's stale copies. Read-only on the VM: copies five files, changes nothing.
#
#   bash scripts/pull_dashboard_data.sh
#   ALPHA_DATA_DIR=data/vm_mirror streamlit run src/dashboard/app.py
set -euo pipefail
VM="adigupta1998@alpha-trading-vm"
PROJECT="project-37632031-10d0-47dd-b6f"
ZONE="us-central1-a"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/data/vm_mirror"
mkdir -p "$DEST"
for f in data/brain_map.db data/journal.jsonl logs/equity_shadow_journal.jsonl data/market_snapshot.json logs/recon.jsonl; do
  base="$(basename "$f")"
  if gcloud compute scp "${VM}:~/alpha_trading/${f}" "${DEST}/${base}.tmp" \
        --project="${PROJECT}" --zone="${ZONE}" --quiet 2>/dev/null; then
    mv "${DEST}/${base}.tmp" "${DEST}/${base}"
    echo "[pull] ${base} ok"
  else
    rm -f "${DEST}/${base}.tmp"
    echo "[pull] ${base} NOT pulled (absent on the VM or scp failed) — keeping the previous copy if any"
  fi
done
echo "[pull] done → ${DEST}  (ALPHA_DATA_DIR=data/vm_mirror streamlit run src/dashboard/app.py)"
