#!/bin/bash
# ============================================================================
# OFFICE CLOSE (#84, Directive 7) — hit it and walk away.
#
# 1. If it's past 18:30 IST and today's EOD chain (bhavcopy -> pricer ->
#    valuation -> tiers -> VM artifact push) hasn't run, run it NOW.
# 2. Verify the artifacts reached the VM (warn if not — the VM's own
#    freshness gates keep it safe on yesterday's tiers either way).
# 3. Quit the work apps and put the Mac to sleep.
#
# OS-workflow only: touches no trading/sizing/treasury logic.
# ============================================================================
REPO="/Users/adityagupta/Documents/Claude/alpha_trading"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG="$REPO/logs/patience_eod.log"

notify() { osascript -e "display notification \"$1\" with title \"Office Close\""; }

echo "🏢 OFFICE CLOSE — $(date '+%Y-%m-%d %H:%M')"
cd "$REPO" || { notify "repo missing — nothing run"; exit 1; }

# --- 1. the EOD catch-up ----------------------------------------------------
NOW=$(date +%H%M)
RAN_TODAY=$("$PY" -c "
import json, datetime
try:
    a = json.load(open('$REPO/data/darling_tiers.json')).get('as_of', '')
    print('yes' if a[:10] == datetime.date.today().isoformat() else 'no')
except Exception:
    print('no')")

if [ "$RAN_TODAY" = "yes" ]; then
    echo "✅ EOD chain already ran today (tier table is fresh) — no catch-up."
elif [ "$NOW" -lt 1830 ]; then
    echo "⏳ Before 18:30 — NSE bhavcopy isn't out yet, so the chain can't"
    echo "   produce today's data. Skipping (the VM trades safely on"
    echo "   yesterday's tiers — its 3-day freshness gate allows it, and"
    echo "   the 19:15 cron will still fire if the Mac happens to be awake)."
    notify "EOD skipped: before 18:30, bhavcopy not out. VM is safe on yesterday's tiers."
else
    echo "🔄 Running the EOD chain now (bhavcopy → forensics/valuation →"
    echo "   tiers → VM push)… this takes a few minutes."
    "$PY" -m src.analysis.patience_basket --eod >> "$LOG" 2>&1
    # --- 2. verify the ship -------------------------------------------------
    if tail -5 "$LOG" | grep -q "artifacts_shipped.*darling_tiers.json"; then
        echo "✅ EOD chain done — artifacts shipped to the VM."
        notify "EOD chain done, artifacts on the VM. Sleeping the Mac."
    else
        echo "⚠️ Chain ran but the VM ship isn't confirmed — check"
        echo "   logs/patience_eod.log on return. (The VM stays safe: its"
        echo "   freshness gate simply holds new entries on stale tiers.)"
        notify "⚠️ EOD ran but VM push unconfirmed — see patience_eod.log. Sleeping anyway."
    fi
fi

# --- 3. graceful shutdown ---------------------------------------------------
echo "🌙 Closing the office…"
for APP in "Google Chrome" "Visual Studio Code" "Code"; do
    osascript -e "if application \"$APP\" is running then tell application \"$APP\" to quit" 2>/dev/null
done
sleep 2
# Quit Terminal only if this script is NOT running inside it (the
# double-clicked .command runs in Terminal — quitting it also ends this
# window, so sleep is issued first in that case).
if [ "$TERM_PROGRAM" = "Apple_Terminal" ] || [ "$TERM_PROGRAM" = "iTerm.app" ]; then
    ( sleep 3; pmset sleepnow ) &>/dev/null &
    osascript -e 'tell application "Terminal" to quit' 2>/dev/null
    osascript -e 'tell application "iTerm2" to quit' 2>/dev/null
else
    pmset sleepnow
fi
