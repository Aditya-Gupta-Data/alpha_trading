# CRON_SETUP.md — Automating the Live Paper-Trading Session (Mac)

How to make the Phase 7A master scheduler (`src/master_scheduler.py`)
start itself every weekday morning on this Mac. The scheduler is
self-terminating: it waits for the 09:15 IST open if launched early,
supervises the entry + exit loops through the day, and shuts down
cleanly at the 15:30 IST close — so one cron entry per day is all it
needs (no daemon management).

> **Which machine?** The scheduler belongs on the machine that holds the
> live paper state (`data/journal.jsonl`, `data/brain_map.db`) and the
> Dhan `.env` credentials — currently **this Mac**. The GCP VM's own jobs
> (token renewal, alerts, suggestions, sleep phase) are separate and
> already handled by `scripts/setup_cron.sh` — do not duplicate them here.

## 1. One-time checks

```bash
# from the project folder — confirm the module runs by hand first:
cd /Users/adityagupta/Documents/Claude/alpha_trading
python3 -m src.master_scheduler
# (outside market hours it prints "market day over" and exits — that's correct)

# make sure the logs folder exists:
mkdir -p logs
```

**Timezone caveat (important):** macOS `cron` ignores `CRON_TZ` and fires
in the Mac's **system timezone**. The schedule below assumes this Mac is
set to IST (Asia/Kolkata). If the Mac ever moves timezone, adjust the
hour/minute to whatever local time corresponds to 09:10 IST — the
scheduler itself always trades on IST regardless (it carries its own IST
clock), so a wrong cron time only delays/skips the launch, never trades
at wrong hours.

## 2. Install the crontab entry

Open the crontab editor:

```bash
crontab -e
```

Add this single line (all one line), then save and quit:

```cron
10 9 * * 1-5 cd /Users/adityagupta/Documents/Claude/alpha_trading && /usr/bin/env python3 -m src.master_scheduler >> logs/master_scheduler.log 2>&1
```

What it means:
- `10 9 * * 1-5` — 09:10 local time, Monday through Friday. Five minutes
  early on purpose: the scheduler sleeps until 09:15 IST itself, so the
  session starts exactly at the open even if cron fires a bit late.
- `cd … &&` — cron starts in your home folder; the project's relative
  paths (`data/`, `.env`, `logs/`) need the project root.
- `>> logs/master_scheduler.log 2>&1` — everything the session prints
  (proposals, exit signals, shutdown notes) appends to one log file.

Verify it took:

```bash
crontab -l
```

## 3. macOS permissions (first run only)

The first time cron runs anything that touches your files, macOS may
block it silently. Grant **Full Disk Access** to `cron`:

1. System Settings → Privacy & Security → Full Disk Access
2. Click **+**, press `Cmd+Shift+G`, enter `/usr/sbin/cron`, add it,
   toggle it on.

Also note: the Mac must be **awake** at 09:10 — a sleeping Mac runs no
cron jobs. Either keep it plugged in with sleep disabled during market
hours (System Settings → Displays/Energy), or schedule a wake:
`sudo pmset repeat wakeorpoweron MTWRF 09:05:00`.

## 4. Watching it work

```bash
# live tail of today's session:
tail -f logs/master_scheduler.log

# stop a running session cleanly (it also stops itself at 15:30):
pkill -INT -f "src.master_scheduler"
```

A healthy day looks like: a 🟢 session-OPEN card in Discord at 09:15
(account snapshot + the planner's playbook), proposal / exit-signal
alerts during the day, and a 🔴 session-CLOSED card just after 15:30.

## 5. Removing / changing the schedule

```bash
crontab -e    # edit or delete the line, save, done
```
