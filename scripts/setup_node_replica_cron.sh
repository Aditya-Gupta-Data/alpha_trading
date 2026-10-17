#!/bin/bash
# scripts/setup_node_replica_cron.sh — run the VM's WHOLE schedule on the home node, in parallel
# ==============================================================================
# Decision #144 (2026-10-10). Owner directive: for the trial week the Mini PC runs
# EXACTLY what the VM runs, the VM changes in no way, and every night the node
# reconciles itself against the VM and files a report (src/node_reconcile.py).
# From the week after, the node is to replace the VM, so this is the dress rehearsal.
#
# What it installs (a second, separate crontab block — the home-lane block from
# setup_mininode_cron.sh is untouched):
#   * the VM's cron lines, rendered from scripts/setup_cron.sh by
#     scripts/node_replica_block.py so the two can never drift, minus the two
#     jobs a replica must not run (token renewal; the dashboard-mirror push)
#   * ALPHA_NODE_LABEL=minipc1 — every Discord card / email the node sends carries it
#   * scripts/node_heartbeat.sh every minute — the outage record
#   * src.node_reconcile at 23:30 — the nightly report + one Discord card
#   * with --with-token only: scripts/node_token_mirror.sh at 07:15 (see below)
#
# THE TOKEN. Dhan allows ONE active token per account, and the VM renews it. The
# node never renews (that would log the VM out, decision #48). Without --with-token
# the node has no live token and its market-hours jobs abstain with named reasons —
# safe, but the live session is not exercised. --with-token mirrors the VM's current
# token (read-only, one line, never logged) after the 07:00 renewal; that makes the
# node a SECOND CONSUMER of the same account's call budget, so enable it deliberately,
# not on a day the VM's own session is the thing being watched.
#
#   bash scripts/setup_node_replica_cron.sh --dry-run              # print the block, touch nothing
#   bash scripts/setup_node_replica_cron.sh                        # install (no token)
#   bash scripts/setup_node_replica_cron.sh --with-token           # install + the token mirror
#   bash scripts/setup_node_replica_cron.sh --remove               # take the block out
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="install"; WITH_TOKEN=0
for arg in "$@"; do
    case "$arg" in
        --with-token) WITH_TOKEN=1 ;;
        --dry-run|--remove) MODE="$arg" ;;
        *) echo "usage: $0 [--with-token] [--dry-run|--remove]" >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" = "Darwin" ]; then echo "setup_node_replica_cron: this is the LINUX home node's schedule." >&2; exit 1; fi
case "$(hostname)" in
    alpha-trading-vm*) echo "setup_node_replica_cron: refusing to run on the VM." >&2; exit 1 ;;
esac
if [ "$(date +%z)" != "+0530" ]; then
    echo "setup_node_replica_cron: host clock offset is $(date +%z), not +0530. Run: sudo timedatectl set-timezone Asia/Kolkata" >&2; exit 1
fi
PY="$REPO_ROOT/venv/bin/python"
if [ ! -x "$PY" ]; then echo "setup_node_replica_cron: no venv at $PY — run scripts/bootstrap_node.sh first." >&2; exit 1; fi
if [ "$MODE" != "--dry-run" ] && ! command -v crontab >/dev/null 2>&1; then
    echo "setup_node_replica_cron: no crontab binary (sudo apt install -y cron)." >&2; exit 1
fi

BLOCK_START="# === ALPHA TRADING NODE REPLICA BLOCK START (setup_node_replica_cron.sh) ==="
BLOCK_END="# === ALPHA TRADING NODE REPLICA BLOCK END ==="
VM_LINES="$("$PY" "$REPO_ROOT/scripts/node_replica_block.py" --repo "$REPO_ROOT" --python "$PY")"
[ -n "$VM_LINES" ] || { echo "setup_node_replica_cron: rendered no VM lines — setup_cron.sh changed shape?" >&2; exit 1; }

OPS_ENV="$("$PY" "$REPO_ROOT/scripts/node_replica_block.py" --repo "$REPO_ROOT" --ops-env 2>/dev/null || true)"
OPS_LINE=""
if [ -n "$OPS_ENV" ]; then OPS_LINE="OPS_EXPECTED_JOBS=$OPS_ENV"
else echo "setup_node_replica_cron: WARNING — could not derive OPS_EXPECTED_JOBS (src.ops_monitor import failed); the node's 20:30 sweep will report renew_token.log SILENT every night." >&2; fi

TOKEN_LINE=""
if [ "$WITH_TOKEN" -eq 1 ]; then
    TOKEN_LINE="15 7 * * * cd \"$REPO_ROOT\" && bash scripts/node_token_mirror.sh >> \"$REPO_ROOT/logs/node_token_mirror.log\" 2>&1"
fi

CRON_BLOCK="$BLOCK_START
SHELL=/bin/bash
# MODE: REPLICA TRIAL — the VM's schedule, run in parallel; the VM is not touched.
ALPHA_NODE_LABEL=minipc1
ALPHA_NODE_REPLICA=1${OPS_LINE:+
$OPS_LINE}
$VM_LINES
# node-only: the outage record, and the nightly report against the VM
* * * * * cd \"$REPO_ROOT\" && bash scripts/node_heartbeat.sh
30 23 * * * cd \"$REPO_ROOT\" && \"$PY\" -m src.node_reconcile >> \"$REPO_ROOT/logs/node_reconcile.log\" 2>&1${TOKEN_LINE:+
$TOKEN_LINE}
$BLOCK_END"

existing="$(crontab -l 2>/dev/null || true)"
stripped="$(printf '%s\n' "$existing" | awk -v s="$BLOCK_START" -v e="$BLOCK_END" '
    $0 == s {skip=1; next} $0 == e {skip=0; next} !skip {print}')"

case "$MODE" in
    --dry-run) echo "$CRON_BLOCK"; exit 0 ;;
    --remove)
        printf '%s\n' "$stripped" | sed '/^$/N;/^\n$/D' | crontab -
        echo "setup_node_replica_cron: replica block removed."; exit 0 ;;
    install)
        mkdir -p "$REPO_ROOT/logs"
        { printf '%s\n' "$stripped" | sed '/^$/N;/^\n$/D'; echo; echo "$CRON_BLOCK"; } | crontab -
        # hand-run commands on the node carry the label too (discord_client loads .env)
        if [ -f "$REPO_ROOT/.env" ] && ! grep -q '^ALPHA_NODE_LABEL=' "$REPO_ROOT/.env"; then
            printf '\nALPHA_NODE_LABEL=minipc1\n' >> "$REPO_ROOT/.env"; chmod 600 "$REPO_ROOT/.env"
        fi
        echo "setup_node_replica_cron: installed $(echo "$VM_LINES" | wc -l | tr -d ' ') VM job lines + heartbeat + nightly reconcile$([ "$WITH_TOKEN" -eq 1 ] && echo ' + token mirror')."
        [ -f "$REPO_ROOT/data/.node_seed.json" ] || echo "  !! the node has not been seeded from the VM — run: bash scripts/node_seed_from_vm.sh --yes   (remove the block first if jobs are already running)"
        [ "$WITH_TOKEN" -eq 1 ] || echo "  note: no live token on the node — market-hours jobs will abstain. Add --with-token when you are ready (it is a second consumer of the Dhan account's call budget)."
        echo "  tomorrow 23:30: logs/node_reconcile/$(date +%F).md and one [minipc1] Discord card"
        ;;
esac
