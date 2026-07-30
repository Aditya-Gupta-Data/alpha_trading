#!/bin/bash
# ============================================================================
# OFFICE CLOSE — one button: capture, settle the desk, sanitise, sleep.
#
# Merged 2026-07-30 from two tools that had drifted apart and both quit
# Chrome:
#   * this script (#84, Directive 7, 2026-07-21) — EOD chain + VM push + sleep
#   * ~/Applications/Office Close.app (2026-07-30)  — note capture + RAM sweep
# Running only one silently skipped either the VM push or the memory sweep,
# so they are now a single ordered pass.
#
# ORDER IS DELIBERATE: the one interactive step runs FIRST, so everything
# after it is unattended and you can walk away.
#
#   0. Ask what is pending tomorrow -> ~/Desktop/Pending_Tasks.md
#   1. Run today's EOD chain if it has not run (bhavcopy -> pricer ->
#      valuation -> tiers -> VM artifact push)
#   2. Gracefully quit the work apps (standard Quit, never a force kill)
#   3. Kill Ollama + orphaned main.py workers
#   4. Sleep the Mac
#
# NO BACKGROUND JOBS AND NO `tell Terminal to quit`. The previous version
# backgrounded `( sleep 3; pmset sleepnow ) &` and then asked Terminal to
# quit, which made Terminal raise "terminate running processes?" — and the
# default button, Terminate, killed the pending pmset so the Mac never
# slept. Nothing is left running here, so the window closes silently.
#
# OS-workflow only: touches no trading/sizing/treasury logic.
#
# Usage:  ./office_close.command              normal run
#         ./office_close.command --dry-run    print actions, quit nothing,
#                                             sweep nothing, do not sleep
# ============================================================================

# -u catches typos; NO -e on purpose — one failing phase must not skip the
# phases after it (same doctrine as scripts/daily_health_and_queue.sh).
set -uo pipefail

REPO="/Users/adityagupta/Documents/Claude/alpha_trading"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG="$REPO/logs/patience_eod.log"
TRACKER="$HOME/Desktop/Pending_Tasks.md"
JARVIS="$HOME/Scripts/jarvis_parse.py"

# Never sweep these, whatever else matches.
PROTECTED='pytest|wrap_session|uvicorn|brain_mcp'
# Anchored: <pid> <python-binary> [flags] main.py. Anchoring stops a shell
# that merely MENTIONS main.py in its argv from being selected.
ORPHAN_RX='^ *[0-9]+ +[^ ]*[Pp]ython[^ ]*( +-[^ ]+)* +[^ ]*main\.py'

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

notify() { osascript -e "display notification \"$1\" with title \"Office Close\"" >/dev/null 2>&1; }

# `tell application X to quit` LAUNCHES a non-running app, so check first.
# `comm=` is the executable path only — the argument list gives false
# positives because grep's own argv contains the search string.
app_running() { /bin/ps -axo comm= | /usr/bin/grep -qF "/$1.app/Contents/MacOS/"; }

quit_app() {
    if app_running "$1"; then
        if [ "$DRY" = "1" ]; then
            echo "   [dry-run] would quit $1"
        else
            osascript -e "tell application \"$1\" to quit" >/dev/null 2>&1
            echo "   quit $1"
        fi
    fi
}

echo "OFFICE CLOSE — $(date '+%Y-%m-%d %H:%M')"
[ "$DRY" = "1" ] && echo "*** DRY RUN — nothing will be quit, swept or slept ***"
cd "$REPO" || { notify "repo missing — nothing run"; exit 1; }

# --- 0. capture tomorrow's context (the only interactive step) --------------
echo
echo "[0/4] What is pending for tomorrow?"
RESP=$(osascript <<'OSA' 2>/dev/null
try
	display dialog "What work is pending for tomorrow?" default answer "" with title "Office Close" buttons {"Skip", "Save"} default button "Save" with icon note
	return (button returned of result) & "|" & (text returned of result)
on error number -128
	return "CANCEL|"
end try
OSA
)
BTN="${RESP%%|*}"
TXT="${RESP#*|}"

if [ "$BTN" = "CANCEL" ]; then
    echo "   Escape pressed — aborting the whole close. Nothing changed."
    exit 0
fi

if [ "$BTN" = "Save" ] && [ -n "$TXT" ]; then
    # Jarvis: casual English -> structured Markdown. Runs on demand and
    # EXITS — no daemon, no model, no network. FAIL-OPEN: if the parser is
    # missing or throws, the raw note is appended rather than lost.
    BLOCK=""
    [ -f "$JARVIS" ] && BLOCK=$(printf '%s' "$TXT" | /usr/bin/python3 "$JARVIS" 2>/dev/null)
    [ -z "$BLOCK" ] && BLOCK="## $(date '+%Y-%m-%d %H:%M')
- $TXT"
    [ -f "$TRACKER" ] || printf '# Pending Tasks\n' > "$TRACKER"
    printf '\n%s\n' "$BLOCK" >> "$TRACKER"
    echo "   saved to ~/Desktop/Pending_Tasks.md"
else
    echo "   skipped."
fi

# --- 1. the EOD catch-up ----------------------------------------------------
echo
echo "[1/4] EOD chain"
NOW=$(date +%H%M)
RAN_TODAY=$("$PY" -c "
import json, datetime
try:
    a = json.load(open('$REPO/data/darling_tiers.json')).get('as_of', '')
    print('yes' if a[:10] == datetime.date.today().isoformat() else 'no')
except Exception:
    print('no')" 2>/dev/null)

if [ "$RAN_TODAY" = "yes" ]; then
    echo "   already ran today (tier table is fresh) — no catch-up."
elif [ "$NOW" -lt 1830 ]; then
    echo "   before 18:30 — NSE bhavcopy isn't out yet, so the chain cannot"
    echo "   produce today's data. Skipping (the VM trades safely on"
    echo "   yesterday's tiers — its 3-day freshness gate allows it)."
    notify "EOD skipped: before 18:30. VM safe on yesterday's tiers."
elif [ "$DRY" = "1" ]; then
    echo "   [dry-run] would run the EOD chain now."
else
    echo "   >>> RUNNING THE EOD CHAIN — this takes a few minutes.   <<<"
    echo "   >>> KEEP THE LID OPEN. Closing it SLEEPS the Mac and    <<<"
    echo "   >>> pauses this mid-run; it resumes only on wake.       <<<"
    notify "EOD chain running — keep the lid open."
    # caffeinate holds off IDLE sleep for the chain's duration and exits
    # with it (no lingering background job for Terminal to complain about).
    # It does NOT survive a lid close — nothing short of `pmset disablesleep`
    # does, and that is not something to leave switched on.
    caffeinate -i -s "$PY" -m src.analysis.patience_basket --eod >> "$LOG" 2>&1
    if tail -5 "$LOG" | grep -q "artifacts_shipped.*darling_tiers.json"; then
        echo "   done — artifacts shipped to the VM."
        notify "EOD chain done, artifacts on the VM."
    else
        echo "   ran, but the VM ship isn't confirmed — check"
        echo "   logs/patience_eod.log. (The VM stays safe: its freshness"
        echo "   gate holds new entries on stale tiers.)"
        notify "EOD ran but VM push unconfirmed — see patience_eod.log."
    fi
fi

# --- 2. graceful quit -------------------------------------------------------
# Standard Quit event so each app flushes its own session to disk.
# Terminal is deliberately NOT quit: this script is running inside it, and a
# long job may be living in another tab.
echo
echo "[2/4] Quitting work apps"
quit_app "Google Chrome"
quit_app "Visual Studio Code"
quit_app "Claude"
sleep 2

# --- 3. memory sweep --------------------------------------------------------
echo
echo "[3/4] Memory sweep"

# Ollama. NOTE: `ollama stop --all` does not exist — the syntax is
# `ollama stop MODEL`, so enumerate what is loaded and stop each.
if [ "$DRY" = "1" ]; then
    echo "   [dry-run] would stop Ollama models, quit Ollama, pkill llama-server"
else
    /usr/local/bin/ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}' \
        | xargs -r -n1 /usr/local/bin/ollama stop >/dev/null 2>&1
    quit_app "Ollama"
    pkill -f 'llama-server' >/dev/null 2>&1
    echo "   Ollama stopped."
fi

# Orphaned main.py workers. SIGTERM, never -9. `ps` is captured FIRST so the
# grep in the pipeline cannot match its own argv.
PSNAP=$(/bin/ps -axo pid=,command=)
PIDS=$(printf '%s\n' "$PSNAP" \
        | /usr/bin/grep -E "$ORPHAN_RX" \
        | /usr/bin/grep -Ev "$PROTECTED" \
        | /usr/bin/awk '{print $1}')
if [ -z "$PIDS" ]; then
    echo "   no orphaned main.py workers."
elif [ "$DRY" = "1" ]; then
    echo "   [dry-run] would SIGTERM: $(printf '%s' "$PIDS" | tr '\n' ' ')"
else
    printf '%s\n' "$PIDS" | xargs kill 2>/dev/null
    echo "   swept $(printf '%s\n' "$PIDS" | grep -c .) orphan(s)."
fi
echo "   swap used: $(/usr/sbin/sysctl -n vm.swapusage | awk '{print $6}')"

# --- 4. sleep ---------------------------------------------------------------
echo
if [ "$DRY" = "1" ]; then
    echo "[4/4] [dry-run] would sleep the Mac now."
    exit 0
fi
echo "[4/4] Everything is closed. Sleeping in 5s — safe to shut the lid."
sleep 5
pmset sleepnow
