#!/bin/bash
# scripts/bootstrap_node_from_mac.sh — run ON THE MAC: build the home node in one command
# ==============================================================================
# MANUAL OFFLINE TOOL (2026-10-10, decision #143). The Mac can reach the Mini
# PC over SSH (LAN or Tailscale); a cloud session cannot. So this is the one
# command the owner runs, from the Mac's repo checkout:
#
#   ssh-copy-id mini_pc1@192.168.29.158                                    # once: no more password prompts
#   bash scripts/bootstrap_node_from_mac.sh mini_pc1@192.168.29.158
#   bash scripts/bootstrap_node_from_mac.sh mini_pc1@192.168.29.158 --with-ollama
#   bash scripts/bootstrap_node_from_mac.sh mini_pc1@192.168.29.158 --no-install   # stop after the preflight
#
# Use the user@address that already works by hand. On 2026-10-10 that was the
# LAN address with user `mini_pc1` (hostname minipc1); the node's Tailscale
# address 100.72.160.38 only answers once the Mac is on the same tailnet.
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
    echo "usage: bash scripts/bootstrap_node_from_mac.sh user@node-ip [--with-ollama] [--no-install]   (e.g. mini_pc1@192.168.29.158)" >&2
    exit 2
fi
shift
MAC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MAC_REPO" || exit 1

# One ssh for both checks (every ssh is a password prompt unless the Mac's key
# is on the node: `ssh-copy-id user@node-ip` once makes the whole run silent).
echo "==> Reaching $NODE"
probe="$(ssh -o ConnectTimeout=10 "$NODE" 'echo "$(hostname) $(uname -s)"' 2>&1)" || {
    echo "bootstrap_node_from_mac: cannot ssh to $NODE — $probe" >&2
    echo "    Use the address and user that already work by hand (the LAN one, e.g. mini_pc1@192.168.29.158)." >&2
    echo "    A 100.x Tailscale address only answers if the Mac is logged into the same tailnet." >&2
    exit 1
}
echo "    connected: $probe"
case "$probe" in *Darwin*) echo "bootstrap_node_from_mac: $NODE is a Mac, not the Linux node." >&2; exit 1 ;; esac

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
