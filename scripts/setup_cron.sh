#!/bin/bash
# ==============================================================================
# ADiTrader / Alpha Trading — Idempotent Cron Setup Script
# ==============================================================================
# This script configures the local system's cron daemon to run the automated
# trading background tasks at their exact scheduled Indian Standard Times (IST).
#
# Schedules:
#   1. src.renew_token:       07:00 IST daily (THE ONLY token renewal — see below)
#   2. src.main:              15:35 IST (Mon-Fri)
#   3. src.suggest:           08:00 IST (Mon-Fri)
#   4. src.sleep_phase:       20:00 IST daily (off-market Brain Map memory pass)
#   5. src.master_scheduler:  09:10 IST (Mon-Fri)
#   6. src.ops_monitor:       20:30 IST daily
#   7. src.portfolio_report:  every 2h (posts only during market hours —
#                             the script self-gates; off-hours runs exit
#                             quietly). Fires at even hours, so it can
#                             NEVER collide with the 07:00 renewal slot.
#   (src.evolution is deliberately NOT here: it needs a local Ollama, which
#    the VM lacks by design — it is scheduled on the MAC via launchd instead;
#    see scripts/com.alphatrading.evolution.plist + install_evolution_agent.sh.)
#
# Note on #4: the sleep phase needs the machine that holds data/journal.jsonl,
# data/brain_map.db AND a running Ollama server (Phase 10B). On a machine
# without them it is harmless — ingestion/consolidation skip gracefully and
# only the decay step runs (over an empty/local DB).
#
# TIMEZONE (ledger Issue 1, 2026-07-09): Debian's stock cron SILENTLY IGNORES
# the CRON_TZ line below — it only works on cronie (RHEL-family). On a UTC
# host every "IST" schedule fires 5h30m late, which is exactly how the
# 2026-07-09 trading session was missed. The only reliable cross-distro fix
# is the HOST clock being IST, so this script now refuses to install unless
# the system timezone offset is +0530 (see the assertion right below).
#
# TOKEN RENEWAL CADENCE (ledger Issue 10, 2026-07-10 — decision): the
# 07:00 IST job installed here is THE ONLY scheduled token renewal.
# A second renewal cron (root's crontab, every 12h, a leftover from the
# initial 2026-07-06 deployment) raced this one against Dhan's
# one-token-per-account rule and caused the 2026-07-10 "Invalid TOTP"
# failure. Never add a second renewal schedule anywhere (user crontab,
# root crontab, systemd timer, Mac launchd) — see
# docs/token_renewal_cadence.md for the standardization + the root-cron
# removal steps. This script warns below if it can see a root-cron
# duplicate.
# ==============================================================================

set -euo pipefail

# 0a. HARD ASSERTION — the host clock must be IST (+0530) or every schedule
#     below is a lie on Debian cron. Fail loudly with the exact fix.
HOST_UTC_OFFSET="$(date +%z)"
if [ "$HOST_UTC_OFFSET" != "+0530" ]; then
    echo "[Cron Setup] FATAL: host timezone offset is $HOST_UTC_OFFSET, not +0530 (IST)."
    echo "[Cron Setup] Debian cron ignores CRON_TZ, so these schedules would fire 5h30m off."
    echo "[Cron Setup] Fix first:  sudo timedatectl set-timezone Asia/Kolkata && sudo systemctl restart cron"
    echo "[Cron Setup] Then re-run this script."
    exit 1
fi
echo "[Cron Setup] Host timezone offset is +0530 (IST) — OK."

# 0b. SINGLE-RENEWAL-CADENCE GUARD — warn (not fail: sudo may prompt) if a
#     duplicate renew_token schedule exists in root's crontab. `sudo -n`
#     never prompts; if we can't read root's crontab, print the manual check.
if sudo -n crontab -l >/dev/null 2>&1; then
    if sudo -n crontab -l 2>/dev/null | grep -q "renew_token"; then
        echo "[Cron Setup] WARNING: root's crontab ALSO schedules renew_token —"
        echo "[Cron Setup] two renewal cadences race each other (ledger Issue 10)."
        echo "[Cron Setup] Remove it per docs/token_renewal_cadence.md before market hours."
    fi
else
    echo "[Cron Setup] Note: could not read root's crontab non-interactively."
    echo "[Cron Setup] Verify no duplicate renewal exists:  sudo crontab -l | grep renew_token"
fi

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

# 7. Portfolio report card — cron fires every 2h; the SCRIPT only posts
#    during NSE market hours (Mon-Fri 09:15-15:30 IST) and exits quietly
#    otherwise. Even-hour slots never touch the 07:00 renewal minute.
0 */2 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.portfolio_report >> "$REPO_ROOT/logs/portfolio_report.log" 2>&1
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
