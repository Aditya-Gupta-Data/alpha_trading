#!/bin/bash
# scripts/node_token_mirror.sh — copy the VM's CURRENT Dhan access token to the node (read-only on the VM)
# ==============================================================================
# Decision #144. The node must never RENEW a token (one active token per account; a
# second renewal logs the VM out — decision #48). It may only READ the one the VM
# already renewed at 07:00. This reads that one line over gcloud ssh and hands it, on
# stdin, to scripts/node_token_mirror.py, which validates it (well-formed, unexpired)
# and swaps ONLY the DHAN_ACCESS_TOKEN line in the node's .env. Never printed or logged.
#
# Installed by `setup_node_replica_cron.sh --with-token` at 07:15. It makes the node a
# second consumer of the Dhan account's call budget — see that script's header.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
. scripts/node_env.sh
[ "$(uname -s)" = "Darwin" ] && { echo "node_token_mirror: run on the node." >&2; exit 1; }
case "$(hostname)" in alpha-trading-vm*) echo "node_token_mirror: refusing to run on the VM." >&2; exit 1 ;; esac
command -v gcloud >/dev/null 2>&1 || { echo "[$(date '+%F %T')] node_token_mirror: gcloud not found"; exit 1; }

LINE="$(gcloud compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a --quiet \
        --command "grep -m1 '^DHAN_ACCESS_TOKEN=' ~/alpha_trading/.env" 2>/dev/null)"
[ -n "$LINE" ] || { echo "[$(date '+%F %T')] node_token_mirror: VM returned no token line — node keeps its current token"; exit 1; }
printf '%s\n' "$LINE" | "$PY" scripts/node_token_mirror.py
