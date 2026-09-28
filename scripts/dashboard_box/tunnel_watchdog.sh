#!/bin/bash
# tunnel_watchdog.sh — runs ON THE DASHBOARD BOX every 2 minutes from
# alpha-tunnel-watchdog.timer (installed by setup_ui.sh; decision #118).
#
# A free trycloudflare.com "quick tunnel" is dropped by Cloudflare without
# warning — three times in four days by 2026-09-28, once for a whole weekend
# — and `systemctl is-active cloudflared-dashboard` still says "active" while
# cloudflared loops "Tunnel not found". This script:
#   1. PERSISTS the current public URL (parsed from cloudflared's own journal
#      since its last start) to $URL_FILE, world-readable, so the trading
#      VM's push script can read it back and announce a change (Discord).
#   2. DETECTS a dead tunnel two ways — "Tunnel not found"/"Unauthorized" in
#      the last 3 minutes of the journal (definitive), or the public
#      /api/health probe failing on two consecutive ticks (a single 530 is
#      normal for ~15 s after a start, so one miss is never enough).
#   3. RESTARTS cloudflared-dashboard, at most once per 5 minutes, and
#      writes the new URL. Every action is one `logger` line
#      (journalctl -t alpha-tunnel-watchdog).
# Restarting rotates the URL — that is the price of a free tunnel; a named
# tunnel on an owned domain is the real fix.
set -u
UNIT=cloudflared-dashboard
URL_FILE=/opt/alpha_trading/data/tunnel_url.txt
STATE=/run/alpha-tunnel-watchdog
TAG=alpha-tunnel-watchdog

url_since() {   # newest URL cloudflared printed since $1 (a journalctl --since value)
  journalctl -u "$UNIT" --since "$1" --no-pager 2>/dev/null \
    | grep -io 'https://[a-z0-9.-]*\.trycloudflare\.com' | tail -1
}
persist() {
  [ -n "$1" ] || return 0
  if [ "$1" != "$(cat "$URL_FILE" 2>/dev/null)" ]; then
    printf '%s\n' "$1" > "$URL_FILE" && chmod 644 "$URL_FILE"
    logger -t "$TAG" "url $1"
  fi
}

started="$(systemctl show -p ActiveEnterTimestamp --value "$UNIT")"
since="$(date -d "${started:-now}" '+%F %T' 2>/dev/null || date '+%F %T')"
url="$(url_since "$since")"
persist "$url"

dead=0; reason=""
if journalctl -u "$UNIT" --since "-3min" --no-pager 2>/dev/null | grep -q "Tunnel not found\|Unauthorized"; then
  dead=1; reason="tunnel-not-found"
elif [ -n "$url" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/api/health" || echo 000)"
  if [ "$code" = "200" ]; then
    echo 0 > "$STATE.fails"
  else
    f=$(( $(cat "$STATE.fails" 2>/dev/null || echo 0) + 1 )); echo "$f" > "$STATE.fails"
    [ "$f" -ge 2 ] && { dead=1; reason="probe-$code-x$f"; }
  fi
fi
[ "$dead" -eq 0 ] && exit 0

now=$(date +%s); last=$(cat "$STATE.last" 2>/dev/null || echo 0)
if [ $((now - last)) -lt 300 ]; then
  logger -t "$TAG" "dead ($reason) but restarted $((now - last))s ago — waiting"; exit 0
fi
logger -t "$TAG" "restarting $UNIT ($reason)"
systemctl restart "$UNIT"
echo "$now" > "$STATE.last"; echo 0 > "$STATE.fails"
sleep 15
persist "$(url_since "-1min")"
