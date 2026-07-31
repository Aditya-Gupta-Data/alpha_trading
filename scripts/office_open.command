#!/bin/bash
# ============================================================================
# OFFICE OPEN (#84, Directive 7) — the morning glance, read-only.
# VM health + firm treasury + the live equity book + last night's chain
# status + the bug ledger count. Touches nothing, changes nothing.
# ============================================================================
REPO="/Users/adityagupta/Documents/Claude/alpha_trading"
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
GCLOUD="/opt/homebrew/share/google-cloud-sdk/bin/gcloud"
SSH_ARGS=(compute ssh adigupta1998@alpha-trading-vm
          --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a)

echo "☀️ OFFICE OPEN — $(date '+%Y-%m-%d %H:%M')"
echo ""
echo "── Mac (analysis side) ──────────────────────────────"
"$PY" -c "
import json, datetime
try:
    a = json.load(open('$REPO/data/darling_tiers.json')).get('as_of', '?')
    now = datetime.datetime.now()
    today = now.date().isoformat()
    # The EOD chain runs at 19:15. Before then, yesterday's tier table is
    # the CORRECT state — flagging it as a miss all morning trained the
    # eye to ignore a warning that is only meaningful after the cron.
    if str(a)[:10] == today:
        ok = '✅ fresh'
    elif (now.hour, now.minute) < (19, 30):
        ok = 'ok — yesterday\'s; today\'s EOD chain runs 19:15'
    else:
        ok = '⚠️ NOT today and it is past 19:30 — Office Close missed?'
    print(f'  tier table as_of: {a[:16]}  {ok}')
except Exception as e:
    print(f'  tier table unreadable: {e}')"
echo ""
echo "── VM (trading side) ────────────────────────────────"
"$GCLOUD" "${SSH_ARGS[@]}" --command "cd ~/alpha_trading \
  && echo \"  service: \$(systemctl is-active alpha-trading)\" \
  && venv/bin/python -m src.firm_treasury | head -1 | sed 's/^/  /' \
  && venv/bin/python -m src.equity_desk | head -1 | sed 's/^/  /' \
  && echo \"  bug ledger: \$(wc -l < logs/autonomous_bug_report.jsonl 2>/dev/null || echo 0) item(s) collected\"" \
  2>/dev/null | grep -v "^Warning\|^Updating\|^Waiting"
echo ""
echo "Done — the machine runs itself. Read the evening digests. 🤖"
