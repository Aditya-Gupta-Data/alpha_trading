#!/bin/bash
# node_sector_bars.sh — the Mini PC's sector-bars producer (Architect 2026-10-28, decision #160).
# Fetches data/sector_index_bars.json with yfinance (Yahoo blocks datacentre IPs; the node is on a home
# line) and ships it to the VM's data/ through firm_treasury.vm_push_file, the one artifact lane. The
# Mac's auto-sync no longer fetches or ships this file: one machine owns each artifact.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
PY="$REPO_ROOT/venv/bin/python"
[ -x "$PY" ] || PY="python3"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
echo "[$(ts)] sector bars: fetching"
if ! "$PY" scripts/fetch_sector_bars.py; then
    echo "[$(ts)] sector bars: FAILED (stored file kept, nothing shipped)"
    exit 1
fi
if "$PY" -c "import sys; from src import firm_treasury as ft; sys.exit(0 if ft.vm_push_file('data/sector_index_bars.json') else 1)"; then
    echo "[$(ts)] sector bars: shipped to the VM"
else
    echo "[$(ts)] sector bars: ship FAILED (the VM keeps its copy; its staleness guard watches it)"
    exit 1
fi
