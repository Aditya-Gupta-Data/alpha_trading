#!/bin/bash
# ship_ui.sh — build the React desk HERE (the Mac) and rsync the finished Node
# bundle to the dashboard box (decision #113). The box is a 1 GB Oracle
# free VM: a vite/npm build on it thrashes it into an SSH banner timeout
# (seen 2026-09-24, twice), so it never runs npm — it only runs `node`.
#   bash scripts/dashboard_box/ship_ui.sh            # build + ship + restart
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
BOX="${DASHBOARD_BOX:-opc@80.225.235.164}"
KEY="${DASHBOARD_BOX_KEY:-$HOME/.ssh/id_ed25519}"
cd "$HERE/frontend"
[ -d node_modules ] || npm ci --no-audit --no-fund
rm -rf .output
NITRO_PRESET=node-server npm run build >/dev/null
test -f .output/server/index.mjs
rsync -az --delete -e "ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=20" .output/ "$BOX:/opt/alpha_trading/frontend/.output/"
ssh -i "$KEY" -o BatchMode=yes "$BOX" 'sudo restorecon -R /opt/alpha_trading/frontend/.output >/dev/null 2>&1 || true; sudo systemctl restart alpha-desk-ui 2>/dev/null && sleep 3 && systemctl is-active alpha-desk-ui || echo "alpha-desk-ui not installed yet — run scripts/dashboard_box/setup_ui.sh on the box"'
echo "[ship] $(date '+%F %T') desk bundle shipped to $BOX"
