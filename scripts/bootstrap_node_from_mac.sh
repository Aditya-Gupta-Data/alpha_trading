#!/bin/bash
# scripts/bootstrap_node_from_mac.sh — run ON THE MAC: build the home node in one command
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-10-10, decision #143). The Mac can reach the Mini
# PC over SSH (LAN or Tailscale); a cloud session cannot. So this is the one
# command the owner runs, from the Mac's repo checkout:
#
#   bash scripts/bootstrap_node_from_mac.sh minipc1@100.72.160.38
#   bash scripts/bootstrap_node_from_mac.sh minipc1@100.72.160.38 --with-ollama
#   bash scripts/bootstrap_node_from_mac.sh minipc1@100.72.160.38 --no-install   # stop after the preflight
#
# What it does:
#   1. rsyncs THIS checkout (code + .git + config), data/ (the corpus) and
#      .env to ~/alpha_trading on the node — never venv/, never logs/ (the
#      node's logs must be its own, that is what the trial report reads),
#      never the Mac's sync-throttle stamp. Nothing is deleted on the node.
#   2. runs scripts/bootstrap_node.sh there over `ssh -t`, so the node's
#      sudo password and the gcloud login prompt on your terminal.
#
# It never renews or pushes a token and never touches the VM; the node-side
# script strips the Dhan account-control keys from the copied .env (#46).
set -uo pipefail

NODE="${1:-}"
if [ -z "$NODE" ]; then
    echo "usage: bash scripts/bootstrap_node_from_mac.sh user@node-ip [--with-ollama] [--no-install]" >&2
    exit 2
fi
shift
MAC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MAC_REPO" || exit 1

echo "==> Reaching $NODE"
if ! ssh -o ConnectTimeout=10 "$NODE" 'echo "    connected: $(hostname) ($(uname -s))"'; then
    echo "bootstrap_node_from_mac: cannot ssh to $NODE — is the box on, on the LAN/Tailscale, and is the user right?" >&2
    exit 1
fi
if ssh "$NODE" 'uname -s' 2>/dev/null | grep -q Darwin; then
    echo "bootstrap_node_from_mac: $NODE is a Mac, not the Linux node." >&2
    exit 1
fi

echo "==> Sizing what travels (data/ can be a few GB)"
du -sh data 2>/dev/null | sed 's/^/    data: /'
[ -f .env ] && echo "    .env: present" || echo "    .env: MISSING on the Mac — the node will have no secrets"

echo "==> Copying the checkout + data/ + .env → $NODE:~/alpha_trading/  (no deletes on the node)"
rsync -avh --progress \
    --exclude 'venv/' --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
    --exclude 'logs/' --exclude 'data/.mac_auto_sync_state' --exclude '.DS_Store' \
    --exclude 'lovable-frontend/' --exclude 'frontend/node_modules/' --exclude 'archive/' --exclude 'drop/' \
    "$MAC_REPO/" "$NODE:~/alpha_trading/" || { echo "rsync failed" >&2; exit 1; }

echo "==> Running the node-side build (sudo + gcloud login will prompt here)"
exec ssh -t "$NODE" "cd ~/alpha_trading && bash scripts/bootstrap_node.sh $*"
