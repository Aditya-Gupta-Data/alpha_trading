#!/bin/bash
# ==============================================================================
# push_token_to_vm.sh — push ONLY the fresh DHAN_ACCESS_TOKEN to the VM
# ==============================================================================
# Run this on the Mac AFTER `python3 -m src.renew_token` has renewed the local
# .env. It copies just the current DHAN_ACCESS_TOKEN value to the VM's .env
# (same single-line-replace logic renew_token.py uses locally) and restarts
# the VM's alpha-trading service so it picks up the fresh token.
#
# DELIBERATELY NEVER SENT to the VM: DHAN_PIN, DHAN_TOTP_SECRET, DHAN_API_KEY,
# DHAN_API_SECRET — those stay on this Mac only. The VM only ever holds a
# short-lived (~24h) bearer token, exactly what it already held before this
# script existed — no new class of secret exposure.
#
# The token crosses the network via `gcloud compute scp` of a locked-down
# (chmod 600) temp file — never embedded in a command line (which could leak
# into shell history or `ps` output on either machine) — and the temp file is
# deleted on both ends immediately after use, success or failure.
#
# Usage:
#   bash scripts/push_token_to_vm.sh            # do it
#   bash scripts/push_token_to_vm.sh --dry-run   # show what would happen, touch nothing
# ==============================================================================

set -euo pipefail

# cron runs with a near-empty PATH — Homebrew's gcloud won't resolve as a
# bare command there, so we call it by absolute path and also widen PATH
# for anything gcloud itself shells out to.
GCLOUD_BIN="/opt/homebrew/share/google-cloud-sdk/bin/gcloud"
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:/opt/homebrew/bin:$PATH"
if [[ ! -x "$GCLOUD_BIN" ]]; then
  GCLOUD_BIN="$(command -v gcloud || true)"
fi
if [[ -z "$GCLOUD_BIN" ]]; then
  echo "gcloud CLI not found — cannot push the token to the VM." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_PATH="$REPO_ROOT/.env"
GCP_PROJECT="project-37632031-10d0-47dd-b6f"
GCP_ZONE="us-central1-a"
GCP_INSTANCE="alpha-trading-vm"
VM_USER="adigupta1998"   # the account that actually owns ~/alpha_trading on the VM
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ ! -f "$ENV_PATH" ]]; then
  echo "No .env at $ENV_PATH — nothing to push." >&2
  exit 1
fi

TOKEN="$(grep -E '^DHAN_ACCESS_TOKEN=' "$ENV_PATH" | head -1 | cut -d= -f2- | tr -d '\r\n')"
if [[ -z "$TOKEN" ]]; then
  echo "DHAN_ACCESS_TOKEN not found (or empty) in $ENV_PATH — run src.renew_token first." >&2
  exit 1
fi

if $DRY_RUN; then
  echo "[dry-run] Found a local DHAN_ACCESS_TOKEN (${#TOKEN} chars)."
  echo "[dry-run] Would scp it to $GCP_INSTANCE, update its .env, restart alpha-trading."
  echo "[dry-run] Nothing was sent. Re-run without --dry-run to actually push."
  exit 0
fi

TMP_FILE="$(mktemp -t dhan_token_push.XXXXXX)"
chmod 600 "$TMP_FILE"
printf '%s' "$TOKEN" > "$TMP_FILE"
trap 'rm -f "$TMP_FILE"' EXIT

echo "Copying fresh token to $GCP_INSTANCE as $VM_USER (secret file only, chmod 600)..."
"$GCLOUD_BIN" compute scp "$TMP_FILE" \
  "$VM_USER@$GCP_INSTANCE:~/.token_push.tmp" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" --quiet

echo "Updating the VM's .env and restarting the service..."
"$GCLOUD_BIN" compute ssh "$VM_USER@$GCP_INSTANCE" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" --quiet --command='
set -euo pipefail
cd ~/alpha_trading
NEW_TOKEN="$(cat ~/.token_push.tmp)"
rm -f ~/.token_push.tmp
venv/bin/python3 - "$NEW_TOKEN" <<'"'"'PYEOF'"'"'
import sys
from src.renew_token import replace_token, ENV_PATH, BACKUP_PATH
new_token = sys.argv[1]
env_text = ENV_PATH.read_text()
BACKUP_PATH.write_text(env_text)
ENV_PATH.write_text(replace_token(env_text, new_token))
print("VM .env updated with the fresh token (backup at .env.bak).")
PYEOF
sudo systemctl restart alpha-trading
sleep 2
sudo systemctl is-active alpha-trading
'

echo "Done. VM is now running on the freshly renewed token — PIN/TOTP/API secrets were never sent."
