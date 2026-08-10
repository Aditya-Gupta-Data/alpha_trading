#!/bin/bash
# scripts/mac_auto_sync.sh — the Mac's background dependency sync
# ==============================================================================
# WHY THIS EXISTS (2026-08-11). Three artifacts can only be BUILT on the Mac
# and are only USEFUL on the VM:
#
#   sector_index_bars.json  yfinance. Yahoo rate-limits/blocks datacentre IPs
#                           and `src/` must stay free of a yfinance import, so
#                           scripts/fetch_sector_bars.py says MAC-ONLY, NEVER
#                           RUN ON THE VM — and it means it. Feeds the router's
#                           momentum leg and a LIVE bullish veto.
#   darlings_valuation.json needs the fundamentals + deep-read corpus, which
#                           lives on the Mac. Measured on the VM 2026-08-11:
#                           0 of 109 darlings scored, and the empty file it
#                           writes greys out EVERY darling on the next grading
#                           pass. This is the one that bites.
#   bars_cache.json         off the live entry path (evolution / regime
#                           backfill), but it had no schedule anywhere, so the
#                           ops sweep flagged it at 31 days.
#
# The old arrangement made these a side effect of the Mac's 19:15 EOD chain,
# so a laptop that stayed shut meant the VM quietly aged. By 2026-08-10
# darling_tiers and darlings_levels were 5.1 days stale and the equity desk
# FAILS CLOSED on a stale tier table — days of no darling entries at all.
# This script decouples the two: the VM builds what it can from its own
# bhavcopy (pricer + tiers, cron 19:18/19:22), and the Mac pushes only what
# the VM genuinely cannot compute, whenever the Mac happens to be awake.
#
# WHAT IT DELIBERATELY DOES NOT SHIP: darling_tiers.json and
# darlings_levels.json. Those became VM-NATIVE on 2026-08-11 and the VM's copy
# is built off its own same-day bhavcopy. Pushing a Mac copy over them would
# reintroduce exactly the staleness this script exists to end.
#
# Every stage FAILS OPEN and is idempotent: a dead Yahoo, an expired gcloud
# session or a missing corpus costs that one artifact, never the run.
#
# INSTALL AS A BACKGROUND AGENT: see scripts/com.aditrader.sync.plist.
# RUN BY HAND:  bash scripts/mac_auto_sync.sh [--force]
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
mkdir -p logs

# Interpreter PINNED, never resolved from PATH. Standing lesson from three
# separate unpinned-interpreter incidents in 48h (Mac cron picked
# CommandLineTools python; VM cron picked a bare python3; the edge-miner
# LaunchAgent picked a package-less Homebrew python). Same pin as
# scripts/run_evolution.sh and scripts/mine_edges.sh.
PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
export PATH="/opt/homebrew/bin:/opt/homebrew/share/google-cloud-sdk/bin:$PATH"

STAMP="data/.mac_auto_sync_state"
MIN_GAP_MINUTES=180        # the agent may fire hourly; the work is daily
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ---------------------------------------------------------------- throttle
# launchd fires this at login and then on an interval, so it can wake several
# times an hour. These producers are DAILY work: a yfinance sweep and a
# valuation pass every 60 minutes would be rude to Yahoo and pointless for us.
if [ "$FORCE" -eq 0 ] && [ -f "$STAMP" ]; then
    last=$(cat "$STAMP" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ $(( (now - last) / 60 )) -lt "$MIN_GAP_MINUTES" ]; then
        log "skip: last sync $(( (now - last) / 60 ))m ago (< ${MIN_GAP_MINUTES}m). --force overrides."
        exit 0
    fi
fi

log "=== mac auto-sync starting (py: $PY) ==="

# ------------------------------------------------------------- 1. producers
# Sector index bars — the router's momentum leg and the live bullish veto.
# The script's own merge rule is extend-forward-never-rewrite, so a bad fetch
# can shorten nothing.
log "sector bars: fetching"
"$PY" scripts/fetch_sector_bars.py >> logs/mac_auto_sync.log 2>&1 \
    && log "sector bars: ok" || log "sector bars: FAILED (keeping the stored file)"

# Valuation — Mac-only corpus. NEVER run this on the VM (see the header).
log "valuation: scoring"
"$PY" -m src.analysis.valuation_scorer >> logs/mac_auto_sync.log 2>&1 \
    && log "valuation: ok" || log "valuation: FAILED (keeping the stored file)"

# bars_cache — pulled through the VM's token by evolution.refresh_bars_cache.
# Off the live entry path, and a heavy pull, so it is refreshed WEEKLY rather
# than every run; the staleness guard's own tolerance is 30 days.
if [ "$FORCE" -eq 1 ] || [ -z "$(find data/bars_cache.json -mtime -7 2>/dev/null)" ]; then
    log "bars cache: older than 7d — refreshing through the VM"
    "$PY" - <<'PYEOF' >> logs/mac_auto_sync.log 2>&1
from src.evolution import refresh_bars_cache
print("bars cache refreshed:", refresh_bars_cache())
PYEOF
    log "bars cache: attempted"
else
    log "bars cache: fresh (< 7d) — skipped"
fi

# --------------------------------------------------------------- 2. the ship
# Through firm_treasury.vm_push_file — the ONE Mac→VM artifact lane, which
# already pins the gcloud interpreter and names its failures on stderr rather
# than swallowing them (both fixed 2026-08-05 after it ran dead for 15 days in
# silence). Per-artifact fail-open: the VM freshness-gates everything anyway.
log "shipping to the VM"
"$PY" - <<'PYEOF' 2>&1 | tee -a logs/mac_auto_sync.log
from pathlib import Path
from src import firm_treasury

# NOT darling_tiers.json / darlings_levels.json — those are VM-NATIVE since
# 2026-08-11 (cron 19:18/19:22, built off the VM's own bhavcopy). Shipping a
# Mac copy over them would reintroduce the staleness this script ends.
MANIFEST = ("sector_index_bars.json", "darlings_valuation.json",
            "darlings_queue.json", "darling_pins.json",
            "fo_liquidity.json", "darling_ids.json")

data = Path("data")
ok, failed = [], []
for art in MANIFEST:
    p = data / art
    if not p.exists():
        failed.append(f"{art}:absent")
        continue
    (ok if firm_treasury.vm_push_file(p) else failed).append(art)
print(f"shipped {len(ok)}/{len(MANIFEST)}: {', '.join(ok) or 'none'}")
if failed:
    print(f"NOT shipped: {', '.join(failed)}")
PYEOF

date +%s > "$STAMP"
log "=== mac auto-sync done ==="
