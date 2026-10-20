#!/bin/bash
# scripts/node_heartbeat.sh — one line a minute: "the node was alive and could reach the internet"
# ==============================================================================
# Decision #144 (2026-10-10). The nightly reconcile (src/node_reconcile.py) must
# tell an outage from a bug: when a job that ran on the VM left no trace on the
# node, a gap in THIS log covering the job's start is the proof the box was dark.
#
#   epoch|ISO-time|net=ok|down|up=<uptime seconds>
#
# net is a 3-second TCP connect to a public resolver; "down" with beats still
# arriving means the box was up but offline. Cron itself is the clock: a missed
# minute is a missed beat, which is the point. Self-trims to its last ~2 MB when
# it passes 5 MB. Writes only its own log, touches nothing else, needs no python.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 0
mkdir -p logs
LOG="logs/node_heartbeat.log"
if timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then NET=ok; else NET=down; fi
UP=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
printf '%s|%s|net=%s|up=%s\n' "$(date +%s)" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$NET" "$UP" >> "$LOG"
if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 2097152 "$LOG" | tail -n +2 > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
