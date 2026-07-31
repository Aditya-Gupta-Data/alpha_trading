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

RUNLOG="$REPO/logs/office_close.log"
REFUSED=0

# Everything the run says goes to the terminal AND to a log. Added
# 2026-07-31 after a close that looked fine but left Chrome and Ollama
# running: the quits were `>/dev/null 2>&1` and nothing was written down,
# so the next morning there was no way to tell what had happened. A
# close-down tool that sleeps the Mac ends the session — if it cannot be
# read after the fact, it cannot be debugged at all.
log() { printf '%s\n' "$*" | tee -a "$RUNLOG"; }

notify() { osascript -e "display notification \"$1\" with title \"Office Close\"" >/dev/null 2>&1; }

# `tell application X to quit` LAUNCHES a non-running app, so check first.
# `comm=` is the executable path only — the argument list gives false
# positives because grep's own argv contains the search string.
#
# NO PIPE HERE, and that is the whole point. This was previously
#     /bin/ps -axo comm= | /usr/bin/grep -qF "..."
# which is BROKEN under `set -o pipefail`: `grep -q` exits on the first
# match, `ps` is still writing, `ps` takes SIGPIPE, and the pipeline
# returns 141 — so a RUNNING app reports as not running. Whether it fires
# depends on where the match lands in ps output, i.e. it is a race: on
# 2026-07-30 it reported Chrome and Ollama as absent (they were skipped
# and survived the close) while Claude reported correctly and was quit.
# A `case` on captured output cannot raise SIGPIPE at all.
app_running() {
    case "$(/bin/ps -axo comm=)" in
        *"/$1.app/Contents/MacOS/"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Send Quit, then VERIFY it actually exited. Sending the event and
# assuming it worked is what hid the last failure: an app with an
# unsaved-work prompt (Chrome's "Leave site?") accepts the event, puts up
# a modal, and stays running — silently, and then the Mac slept on top
# of it.
quit_app() {
    app="$1"
    if ! app_running "$app"; then
        log "   $app — not running, skipped"
        return 0
    fi
    if [ "$DRY" = "1" ]; then
        log "   [dry-run] would quit $app"
        return 0
    fi

    err=$(osascript -e "tell application \"$app\" to quit" 2>&1)
    [ -n "$err" ] && log "   $app — osascript said: $err"

    i=0
    while [ "$i" -lt 10 ]; do
        if ! app_running "$app"; then
            log "   $app — quit confirmed (${i}s)"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done

    REFUSED=$((REFUSED + 1))
    log "   !! $app — STILL RUNNING after 10s. It refused to quit."
    log "      Most likely an unsaved-work prompt (Chrome 'Leave site?')"
    log "      or a modal dialog. Nothing was force-killed."
    return 1
}

mkdir -p "$REPO/logs" 2>/dev/null
printf '\n===== %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$RUNLOG"
log "OFFICE CLOSE — $(date '+%Y-%m-%d %H:%M')"
[ "$DRY" = "1" ] && log "*** DRY RUN — nothing will be quit, swept or slept ***"
cd "$REPO" || { notify "repo missing — nothing run"; exit 1; }

# --- 0. capture tomorrow's context (the only interactive step) --------------
log ""
log "[0/4] What is pending for tomorrow?"
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
    log "   Escape pressed — aborting the whole close. Nothing changed."
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
    log "   saved to ~/Desktop/Pending_Tasks.md"
else
    log "   skipped."
fi

# --- 1. the EOD catch-up ----------------------------------------------------
log ""
log "[1/4] EOD chain"
NOW=$(date +%H%M)
RAN_TODAY=$("$PY" -c "
import json, datetime
try:
    a = json.load(open('$REPO/data/darling_tiers.json')).get('as_of', '')
    print('yes' if a[:10] == datetime.date.today().isoformat() else 'no')
except Exception:
    print('no')" 2>/dev/null)

if [ "$RAN_TODAY" = "yes" ]; then
    log "   already ran today (tier table is fresh) — no catch-up."
elif [ "$NOW" -lt 1830 ]; then
    log "   before 18:30 — NSE bhavcopy isn't out yet, so the chain cannot"
    log "   produce today's data. Skipping (the VM trades safely on"
    log "   yesterday's tiers — its 3-day freshness gate allows it)."
    notify "EOD skipped: before 18:30. VM safe on yesterday's tiers."
elif [ "$DRY" = "1" ]; then
    log "   [dry-run] would run the EOD chain now."
else
    log "   >>> RUNNING THE EOD CHAIN — this takes a few minutes.   <<<"
    log "   >>> KEEP THE LID OPEN. Closing it SLEEPS the Mac and    <<<"
    log "   >>> pauses this mid-run; it resumes only on wake.       <<<"
    notify "EOD chain running — keep the lid open."
    # caffeinate holds off IDLE sleep for the chain's duration and exits
    # with it (no lingering background job for Terminal to complain about).
    # It does NOT survive a lid close — nothing short of `pmset disablesleep`
    # does, and that is not something to leave switched on.
    caffeinate -i -s "$PY" -m src.analysis.patience_basket --eod >> "$LOG" 2>&1
    if tail -5 "$LOG" | grep -q "artifacts_shipped.*darling_tiers.json"; then
        log "   done — artifacts shipped to the VM."
        notify "EOD chain done, artifacts on the VM."
    else
        log "   ran, but the VM ship isn't confirmed — check"
        log "   logs/patience_eod.log. (The VM stays safe: its freshness"
        log "   gate holds new entries on stale tiers.)"
        notify "EOD ran but VM push unconfirmed — see patience_eod.log."
    fi
fi

# --- 2. graceful quit -------------------------------------------------------
# Standard Quit event so each app flushes its own session to disk.
# Terminal is deliberately NOT quit: this script is running inside it, and a
# long job may be living in another tab.
log ""
log "[2/4] Quitting work apps"
quit_app "Google Chrome"
quit_app "Visual Studio Code"
quit_app "Claude"
sleep 2

# --- 3. memory sweep --------------------------------------------------------
log ""
log "[3/4] Memory sweep"

# Ollama. NOTE: `ollama stop --all` does not exist — the syntax is
# `ollama stop MODEL`, so enumerate what is loaded and stop each.
if [ "$DRY" = "1" ]; then
    log "   [dry-run] would stop Ollama models, quit Ollama, pkill llama-server"
else
    /usr/local/bin/ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}' \
        | xargs -r -n1 /usr/local/bin/ollama stop >/dev/null 2>&1
    quit_app "Ollama"
    pkill -f 'llama-server' >/dev/null 2>&1
    log "   Ollama stopped."
fi

# Orphaned main.py workers. SIGTERM, never -9. `ps` is captured FIRST so the
# grep in the pipeline cannot match its own argv.
PSNAP=$(/bin/ps -axo pid=,command=)
PIDS=$(printf '%s\n' "$PSNAP" \
        | /usr/bin/grep -E "$ORPHAN_RX" \
        | /usr/bin/grep -Ev "$PROTECTED" \
        | /usr/bin/awk '{print $1}')
if [ -z "$PIDS" ]; then
    log "   no orphaned main.py workers."
elif [ "$DRY" = "1" ]; then
    log "   [dry-run] would SIGTERM: $(printf '%s' "$PIDS" | tr '\n' ' ')"
else
    printf '%s\n' "$PIDS" | xargs kill 2>/dev/null
    log "   swept $(printf '%s\n' "$PIDS" | grep -c .) orphan(s)."
fi
log "   swap used: $(/usr/sbin/sysctl -n vm.swapusage | awk '{print $6}')"

# --- 4. sleep ---------------------------------------------------------------
log ""
if [ "$DRY" = "1" ]; then
    log "[4/4] [dry-run] would sleep the Mac now."
    exit 0
fi
if [ "$REFUSED" -gt 0 ]; then
    log "[4/4] $REFUSED app(s) REFUSED to quit — see above and $RUNLOG."
    log "      Sleeping anyway in 5s; deal with them tomorrow."
    notify "$REFUSED app(s) refused to quit — see logs/office_close.log"
else
    log "[4/4] Everything is closed. Sleeping in 5s — safe to shut the lid."
fi
sleep 5
pmset sleepnow
