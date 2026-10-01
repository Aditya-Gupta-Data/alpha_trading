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
# #123 (audit D10): brain_map.db and journal.jsonl are copied from a
# CONSISTENT SNAPSHOT taken on the VM first (sqlite backup / under the
# journal lock), never the live files mid-commit.
SNAP=/tmp/pull_dashboard_snapshot
REMOTE=('alpha_trading/logs/equity_shadow_journal.jsonl' 'alpha_trading/data/market_snapshot.json'
        'alpha_trading/logs/recon.jsonl' 'alpha_trading/data/dashboard_benchmarks.json')
if gcloud compute ssh "${VM}" --project="${PROJECT}" --zone="${ZONE}" --quiet \
      --command "rm -rf ${SNAP} && cd ~/alpha_trading && venv/bin/python -m src.dashboard.mirror_snapshot ${SNAP}" >/dev/null 2>&1; then
  REMOTE=("${SNAP}/brain_map.db" "${SNAP}/journal.jsonl" "${REMOTE[@]}")
else
  echo "[pull] snapshot on the VM failed — brain_map.db/journal.jsonl NOT pulled (previous copies kept)"
fi
for f in "${REMOTE[@]}"; do
  base="$(basename "$f")"
  if gcloud compute scp "${VM}:${f}" "${DEST}/${base}.tmp" \
        --project="${PROJECT}" --zone="${ZONE}" --quiet 2>/dev/null; then
    mv "${DEST}/${base}.tmp" "${DEST}/${base}"
    echo "[pull] ${base} ok"
  else
    rm -f "${DEST}/${base}.tmp"
    echo "[pull] ${base} NOT pulled (absent on the VM or scp failed) — keeping the previous copy if any"
  fi
done
echo "[pull] done → ${DEST}  (ALPHA_DATA_DIR=data/vm_mirror streamlit run src/dashboard/app.py)"
