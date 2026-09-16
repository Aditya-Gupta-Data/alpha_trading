#!/bin/bash
# scripts/setup_mininode_cron.sh — the HOME NODE's schedule (Linux Mini PC)
# ==============================================================================
# Decision #99 (2026-09-16). The always-on Mini PC replaces the Mac's role:
# every job below is one the Mac used to run from a LaunchAgent or its
# crontab, and every one of them either needs a HOME IP (NSE walls the
# VM's datacentre address), a local Ollama, or the corpus that only this
# lane holds. The VM's schedule (scripts/setup_cron.sh) is untouched.
#
# THIS SCRIPT NEVER: renews the Dhan token (decision #48 — the VM owns the
# one active token), pushes a token, or installs anything from the VM's
# crontab. It refuses to run on macOS and on the VM itself.
#
#   IST      job                                       replaces
#   07:30    scripts/mac_auto_sync.sh                  Mac LaunchAgent com.aditrader.sync
#   12:30    scripts/mac_auto_sync.sh                    (login + hourly, 180-min throttle)
#   19:20    scripts/mac_auto_sync.sh                    — after NSE's ~19:00 F&O bundle
#   21:00    scripts/mine_edges.sh                     Mac LaunchAgent alpha-edge-miner
#   Sat 02:00 scripts/run_evolution.sh                 Mac LaunchAgent com.alphatrading.evolution
#   Sat 09:30 src.ingestion.scrip_master              Mac crontab
#   Sat 10:00 src.analysis.weekly_recalibration       Mac crontab (missed 6 weeks asleep)
#
# The sync script keeps its own 180-minute throttle, so three fixed slots a
# day is the intended shape; `--force` is only for hand runs.
#
# Usage (on the Mini PC, from the repo root, clock must be IST):
#   bash scripts/setup_mininode_cron.sh            # install / replace the block
#   bash scripts/setup_mininode_cron.sh --dry-run  # print the block, touch nothing
#   bash scripts/setup_mininode_cron.sh --remove   # take the block out
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-install}"

if [ "$(uname -s)" = "Darwin" ]; then
    echo "setup_mininode_cron: this is the LINUX home node's schedule — the Mac uses LaunchAgents (CRON_SETUP.md)." >&2
    exit 1
fi
case "$(hostname)" in
    alpha-trading-vm*) echo "setup_mininode_cron: refusing to run on the VM — its schedule is scripts/setup_cron.sh." >&2; exit 1 ;;
esac
if [ "$(date +%z)" != "+0530" ]; then
    echo "setup_mininode_cron: host clock offset is $(date +%z), not +0530 (IST). Set the timezone first:" >&2
    echo "    sudo timedatectl set-timezone Asia/Kolkata" >&2
    exit 1
fi
if ! command -v crontab >/dev/null 2>&1; then
    echo "setup_mininode_cron: no crontab binary — install cron first (sudo apt install -y cron)." >&2
    exit 1
fi

# The scripts resolve their own interpreter through scripts/node_env.sh, so
# the cron lines only need bash, the repo path and a log.
BLOCK_START="# === ALPHA TRADING HOME NODE BLOCK START (setup_mininode_cron.sh) ==="
BLOCK_END="# === ALPHA TRADING HOME NODE BLOCK END ==="
read -r -d '' CRON_BLOCK <<EOF || true
$BLOCK_START
SHELL=/bin/bash
# 1. Home-node producers + the 7-file ship to the VM (sector bars, valuation,
#    F&O bundle, darling ids, bars cache). 3 slots/day; the script throttles itself.
30 7 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
30 12 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
20 19 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
# 2. Edge miner (local Ollama; self-gates on >20h since last success).
0 21 * * * cd "$REPO_ROOT" && bash scripts/mine_edges.sh >> "$REPO_ROOT/logs/edge_miner.cron.log" 2>&1
# 3. Procedural evolution (local Ollama; never auto-applies anything).
0 2 * * 6 cd "$REPO_ROOT" && bash scripts/run_evolution.sh >> "$REPO_ROOT/logs/evolution.cron.log" 2>&1
# 4. Saturday fundamentals clock — scrip reconciliation, then the weekly re-screen.
30 9 * * 6 cd "$REPO_ROOT" && . scripts/node_env.sh && "\$PY" -m src.ingestion.scrip_master >> "$REPO_ROOT/logs/scrip_master.log" 2>&1
0 10 * * 6 cd "$REPO_ROOT" && . scripts/node_env.sh && "\$PY" -m src.analysis.weekly_recalibration >> "$REPO_ROOT/logs/weekly_recalibration.log" 2>&1
$BLOCK_END
EOF

existing="$(crontab -l 2>/dev/null || true)"
stripped="$(printf '%s\n' "$existing" | awk -v s="$BLOCK_START" -v e="$BLOCK_END" '
    $0 == s {skip=1; next} $0 == e {skip=0; next} !skip {print}')"

case "$MODE" in
    --dry-run)
        echo "$CRON_BLOCK"; exit 0 ;;
    --remove)
        printf '%s\n' "$stripped" | sed '/^$/N;/^\n$/D' | crontab -
        echo "setup_mininode_cron: block removed."; exit 0 ;;
    install)
        mkdir -p "$REPO_ROOT/logs"
        { printf '%s\n' "$stripped" | sed '/^$/N;/^\n$/D'; echo; echo "$CRON_BLOCK"; } | crontab -
        echo "setup_mininode_cron: installed $(echo "$CRON_BLOCK" | grep -c '^[0-9*]') job lines for $(hostname) at $REPO_ROOT"
        crontab -l | grep -c "mac_auto_sync.sh" | sed 's/^/  sync slots: /'
        ;;
    *)
        echo "usage: $0 [--dry-run|--remove]" >&2; exit 2 ;;
esac
