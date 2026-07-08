#!/bin/bash
# ==============================================================================
# ADiTrader / Alpha Trading — Idempotent Cron Setup Script
# ==============================================================================
# This script configures the local system's cron daemon to run the automated
# trading background tasks at their exact scheduled Indian Standard Times (IST).
#
# Schedules:
#   1. src.renew_token: 07:00 IST daily
#   2. src.main:        15:35 IST (Mon-Fri)
#   3. src.suggest:     08:00 IST (Mon-Fri)
#   4. src.sleep_phase: 20:00 IST daily (off-market Brain Map memory pass)
#
# Note on #4: the sleep phase needs the machine that holds data/journal.jsonl,
# data/brain_map.db AND a running Ollama server (Phase 10B). On a machine
# without them it is harmless — ingestion/consolidation skip gracefully and
# only the decay step runs (over an empty/local DB). Timezone: CRON_TZ below
# pins all schedules to IST on Linux (cronie/Vixie); macOS cron ignores
# CRON_TZ and uses the system timezone instead — fine when the Mac is on IST.
# ==============================================================================

set -euo pipefail

# 1. Resolve repo root path dynamically
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "[Cron Setup] Resolved project root to: $REPO_ROOT"

# 2. Ensure logs directory exists
mkdir -p "$REPO_ROOT/logs"

# 3. Locate the Python interpreter
PYTHON_BIN="$REPO_ROOT/venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
    echo "[Cron Setup] Warning: Virtual environment python not found at $REPO_ROOT/venv/bin/python. Falling back to system python3."
else
    echo "[Cron Setup] Using virtual environment python: $PYTHON_BIN"
fi

# 4. Generate the new alpha_trading cron blocks
# We use CRON_TZ=Asia/Kolkata to ensure the schedule runs in IST on any VM.
CRON_BLOCK_START="# === ALPHA TRADING CRON BLOCK START ==="
CRON_BLOCK_END="# === ALPHA TRADING CRON BLOCK END ==="

CRON_ENTRIES=$(cat <<EOF
$CRON_BLOCK_START
# Active schedule in Asia/Kolkata (IST) timezone
CRON_TZ=Asia/Kolkata

# 1. Auto-renew DhanHQ access token (Daily at 07:00 AM IST)
0 7 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.renew_token >> "$REPO_ROOT/logs/renew_token.log" 2>&1

# 2. Run watchlist alert checks (Mon-Fri at 15:35 IST / 3:35 PM IST)
35 15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.main >> "$REPO_ROOT/logs/main.log" 2>&1

# 3. Run daily momentum/trend suggestions (Mon-Fri at 08:00 AM IST)
0 8 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.suggest >> "$REPO_ROOT/logs/suggest.log" 2>&1

# 4. Sleep Phase — off-market Brain Map memory pass (Daily at 20:00 IST)
#    Ingests journal text via local Ollama, consolidates themes, applies decay.
#    On the VM (no Ollama) this gracefully degrades to the decay-only pass;
#    causal-edge mining happens opportunistically from the Mac (src/edge_miner.py).
0 20 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.sleep_phase >> "$REPO_ROOT/logs/sleep_phase.log" 2>&1

# 5. Phase 7A master scheduler — full automated paper-trading session
#    (Mon-Fri, fires 09:10 IST, waits for the 09:15 open, self-terminates 15:30)
10 9 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.master_scheduler >> "$REPO_ROOT/logs/master_scheduler.log" 2>&1

# 6. Nightly ops sweep — log problems + job heartbeats -> Discord health card
30 20 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ops_monitor >> "$REPO_ROOT/logs/ops_monitor.log" 2>&1
$CRON_BLOCK_END
EOF
)

# 5. Read existing crontab, excluding any previous alpha_trading blocks
EXISTING_CRON=""
if crontab -l &>/dev/null; then
    # Filter out any text between start and end blocks (inclusive)
    EXISTING_CRON=$(crontab -l | sed "/$CRON_BLOCK_START/,/$CRON_BLOCK_END/d" || true)
fi

# Trim leading/trailing blank lines in a highly cross-platform way
EXISTING_CRON=$(echo "$EXISTING_CRON" | awk '/./{p=1} p' || true)

# 6. Merge and install new crontab
NEW_CRONTAB=""
if [ -z "$EXISTING_CRON" ]; then
    NEW_CRONTAB="$CRON_ENTRIES"
else
    NEW_CRONTAB="$EXISTING_CRON"$'\n\n'"$CRON_ENTRIES"
fi

echo "$NEW_CRONTAB" | crontab -

echo "[Cron Setup] Success! The following schedules have been installed in crontab:"
echo "--------------------------------------------------------------------------------"
crontab -l | sed -n "/$CRON_BLOCK_START/,/$CRON_BLOCK_END/p"
echo "--------------------------------------------------------------------------------"
