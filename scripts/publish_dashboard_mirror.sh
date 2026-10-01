#!/bin/bash
# publish_dashboard_mirror.sh — runs ON THE TRADING VM (cron #33, decision #112):
# push the five ledger files the showcase reads to the always-on dashboard box
# over ssh/rsync (a dedicated key, ~/.ssh/dashboard_push, that can only log in
# as the dashboard user). Read-only on the engine's files; the box never
# connects back. Target set in ~/.dashboard_target (user@host:/path), one line.
#   bash scripts/publish_dashboard_mirror.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${DASHBOARD_RSYNC_TARGET:-$(cat "$HOME/.dashboard_target" 2>/dev/null || true)}"
KEY="${DASHBOARD_PUSH_KEY:-$HOME/.ssh/dashboard_push}"
if [ -z "$TARGET" ]; then echo "[publish] no target configured (~/.dashboard_target) — nothing pushed"; exit 0; fi
# decision #119: refresh the benchmark closes (lake reads only) before the push
PY0="${PYTHON_BIN:-$HERE/venv/bin/python}"; [ -x "$PY0" ] || PY0=python3
( cd "$HERE" && "$PY0" -m src.dashboard.benchmarks ) 2>&1 | tail -1
files=()
# D10 (decision #123): the two files the engine writes concurrently are
# published from CONSISTENT SNAPSHOTS — brain_map.db via sqlite's online
# backup API, journal.jsonl copied under the journal lock — never the live
# file mid-commit. A snapshot that fails is skipped (the box keeps its last
# good copy); the live file is never pushed in its place.
SNAP="$(mktemp -d "${TMPDIR:-/tmp}/dashboard_mirror.XXXXXX")"
trap 'rm -rf "$SNAP"' EXIT
( cd "$HERE" && "$PY0" -m src.dashboard.mirror_snapshot "$SNAP" ) 2>&1 | grep -v "^$SNAP/" || true
for f in brain_map.db journal.jsonl; do
  [ -f "$SNAP/$f" ] && files+=("$SNAP/$f") || echo "[publish] no consistent snapshot of $f — not pushed"
done
for f in logs/equity_shadow_journal.jsonl data/market_snapshot.json logs/recon.jsonl data/dashboard_benchmarks.json; do
  [ -f "$HERE/$f" ] && files+=("$HERE/$f") || echo "[publish] absent $f"
done
# --temp-dir + delay-updates: the box never reads a half-written file
if rsync -az --timeout=60 --delay-updates -e "ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new" "${files[@]}" "$TARGET/" 2>&1; then
  echo "[publish] $(date '+%F %T') ${#files[@]} file(s) -> $TARGET"
else
  echo "[publish] $(date '+%F %T') PUSH FAILED -> $TARGET"
fi
# Decision #118: the box's tunnel watchdog keeps the CURRENT public URL in a
# one-line file; read it back and let src.dashboard_link remember it and fire
# one 🔗 card when it changed. Fail-open: no file / no ssh = nothing announced.
URL_FILE="${DASHBOARD_URL_FILE:-/opt/alpha_trading/data/tunnel_url.txt}"
PY="${PYTHON_BIN:-$HERE/venv/bin/python}"; [ -x "$PY" ] || PY=python3
BOX_URL="$(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "${TARGET%%:*}" "cat $URL_FILE" 2>/dev/null | head -1)"
if [ -n "$BOX_URL" ]; then
  ( cd "$HERE" && "$PY" -m src.dashboard_link --url "$BOX_URL" ) 2>&1 | sed 's/^/[link] /'
fi
