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
#   IST       job                                      replaces
#   07:30     scripts/mac_auto_sync.sh                 Mac LaunchAgent com.aditrader.sync
#   12:30     scripts/mac_auto_sync.sh                   (login + hourly, 180-min throttle)
#   19:20     scripts/mac_auto_sync.sh                   — after NSE's ~19:00 F&O bundle
#   @reboot   scripts/mac_auto_sync.sh (+90 s)         catch-up after a power cut (10-10)
#   21:00     scripts/mine_edges.sh                    Mac LaunchAgent alpha-edge-miner
#   @reboot   scripts/mine_edges.sh (+120 s)           catch-up; the miner self-gates >20 h
#   Sat 02:00 scripts/run_evolution.sh                 Mac LaunchAgent com.alphatrading.evolution
#   Sat 09:30 src.ingestion.scrip_master               Mac crontab
#
# NOT here any more: src.analysis.weekly_recalibration. Architect ruling E3
# (2026-10-09, decision #139) moved it to the VM, cron #36 Friday 22:00,
# because the No-Orphan pins must be built against the VM's live journal.
# A node copy would run a second recalibration on stale data every week.
#
# The sync script keeps its own 180-minute throttle, so three fixed slots a
# day is the intended shape; `--force` is only for hand runs. The @reboot
# lines exist because plain cron never runs a job it slept through: a box
# that was dark at 07:30 and back at 09:00 would otherwise wait for 12:30.
# Both @reboot jobs are safe to fire at any boot — the sync throttles
# itself and the miner self-gates — so a UPS-protected box that reboots
# twice a day costs nothing extra.
#
# SHADOW TRIAL (2026-10-10, decision #143). `--shadow` installs the SAME
# block with ALPHA_NODE_SHADOW=1 in the crontab environment: every job does
# its full work on the node, but the sync SHIPS NOTHING to the VM, the
# miner APPLIES NOTHING on the VM and the scrip master posts NO Discord
# card. The Mac keeps owning the lane for the trial week; the node proves
# it can keep the schedule (`python3 scripts/node_trial_report.py`). When
# the week reads clean, re-run WITHOUT --shadow to promote the node, then
# retire the Mac's agents (CRON_SETUP.md).
#
# Usage (on the Mini PC, from the repo root, clock must be IST):
#   bash scripts/setup_mininode_cron.sh --shadow   # parallel trial: full work, no writes to the VM
#   bash scripts/setup_mininode_cron.sh            # the real thing (promotion after the trial)
#   bash scripts/setup_mininode_cron.sh --dry-run  # print the block, touch nothing
#   bash scripts/setup_mininode_cron.sh --remove   # take the block out
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="install"
SHADOW=0
for arg in "$@"; do
    case "$arg" in
        --shadow)  SHADOW=1 ;;
        --dry-run|--remove) MODE="$arg" ;;
        *) echo "usage: $0 [--shadow] [--dry-run|--remove]" >&2; exit 2 ;;
    esac
done

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
if [ "$MODE" != "--dry-run" ] && ! command -v crontab >/dev/null 2>&1; then
    echo "setup_mininode_cron: no crontab binary — install cron first (sudo apt install -y cron)." >&2
    exit 1
fi

# The scripts resolve their own interpreter through scripts/node_env.sh, so
# the cron lines only need bash, the repo path and a log.
BLOCK_START="# === ALPHA TRADING HOME NODE BLOCK START (setup_mininode_cron.sh) ==="
BLOCK_END="# === ALPHA TRADING HOME NODE BLOCK END ==="
if [ "$SHADOW" -eq 1 ]; then
    MODE_LINES="# MODE: SHADOW TRIAL — full work, NOTHING shipped/applied/posted; the Mac still owns the lane.
ALPHA_NODE_SHADOW=1"
    SCRIP_QUIET=" --quiet"
else
    MODE_LINES="# MODE: LIVE — this node owns the home lane (the Mac's agents must be retired, CRON_SETUP.md).
ALPHA_NODE_SHADOW=0"
    SCRIP_QUIET=""
fi
read -r -d '' CRON_BLOCK <<EOF2 || true
$BLOCK_START
SHELL=/bin/bash
$MODE_LINES
# 1. Home-node producers + the 7-file ship to the VM (sector bars, valuation,
#    F&O bundle, darling ids, bars cache). 3 slots/day; the script throttles itself.
30 7 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
30 12 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
20 19 * * * cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
#    Catch-up after a power cut / reboot (cron never replays a missed slot; the 180-min throttle makes this free).
@reboot sleep 90 && cd "$REPO_ROOT" && bash scripts/mac_auto_sync.sh >> "$REPO_ROOT/logs/mac_auto_sync.cron.log" 2>&1
# 2. Edge miner (local Ollama; self-gates on >20h since last success). Same catch-up at boot.
0 21 * * * cd "$REPO_ROOT" && bash scripts/mine_edges.sh >> "$REPO_ROOT/logs/edge_miner.cron.log" 2>&1
@reboot sleep 120 && cd "$REPO_ROOT" && bash scripts/mine_edges.sh >> "$REPO_ROOT/logs/edge_miner.cron.log" 2>&1
# 3. Procedural evolution (local Ollama; never auto-applies anything).
0 2 * * 6 cd "$REPO_ROOT" && bash scripts/run_evolution.sh >> "$REPO_ROOT/logs/evolution.cron.log" 2>&1
# 4. Saturday scrip reconciliation. (Weekly recalibration is VM cron #36 since 2026-10-09 — NOT here.)
30 9 * * 6 cd "$REPO_ROOT" && . scripts/node_env.sh && "\$PY" -m src.ingestion.scrip_master$SCRIP_QUIET >> "$REPO_ROOT/logs/scrip_master.log" 2>&1
$BLOCK_END
EOF2

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
        if [ "$SHADOW" -eq 1 ]; then
            echo "setup_mininode_cron: SHADOW TRIAL installed for $(hostname) at $REPO_ROOT — nothing will be shipped, applied or posted."
            echo "  read the week with:  python3 scripts/node_trial_report.py"
        else
            echo "setup_mininode_cron: LIVE schedule installed for $(hostname) at $REPO_ROOT — this node now owns the home lane."
            echo "  retire the Mac's copies now (CRON_SETUP.md, 'Once the node's first sync ships 7/7')."
        fi
        echo "  $(echo "$CRON_BLOCK" | grep -c '^[0-9*@]') job lines; sync slots: $(echo "$CRON_BLOCK" | grep -c '^[0-9]* [0-9]* .*mac_auto_sync.sh')"
        ;;
esac
