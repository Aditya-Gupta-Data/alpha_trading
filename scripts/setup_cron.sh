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
#   8. src.ingestion.deals_tracker: 19:30 IST daily (EOD bulk/block deals
#                             pull; NSE publishes ~19:00, so this lands
#                             after it and before the 20:00 sleep phase).
#   9. src.ingestion.chain_archiver: 15:40 IST Mon-Fri (post-close option
#                             chain capture — after master_scheduler
#                             self-terminates at 15:30, so it never
#                             contends for the single Dhan token).
#  10. src.ingestion.daily_archiver: 19:45 IST daily (snapshot the
#                             perishable news/macro artifacts into the
#                             lake before the 20:00 sleep phase).
#  14. src.portfolio_greeks:  every 2h at :30 IST (posts only during
#                             market hours; self-gates like #7). Book-level
#                             net-Vega/Delta budget advisory, decision #71.
#  15. src.performance:       Saturday 10:05 IST (weekly). Sharpe/Sortino/
#                             max-drawdown over the real paper track record;
#                             abstains silently below the floor. Decision #72.
#  16. src.news_processor:    Daily 19:10 IST. Google-News RSS -> Gemini ->
#                             data/news_sentiment.json (the Gemini path the VM
#                             CAN reach). Was unscheduled; feeds forecast.py.
#  17. src.ingestion.rss_ingester: Daily 18:50 IST. Publishers' own RSS feeds
#                             -> dedup -> classify NEW via Text Intelligence
#                             Manager. Cost-safe by default. Decision #75.
#  18. src.discovery.nightly:  Daily 20:20 IST (decision #76). The GATED
#                             Phase-5 miner pass: skips quietly (exit 0)
#                             unless ops heartbeats are green, the latest
#                             sweep shows no INGESTION problem lines, and
#                             daily_context has >= 40 frames; every 7th
#                             consecutive skip fires one Discord note.
#                             Runs AFTER sleep_phase (20:00, drift monitor
#                             Task H) and BEFORE the 20:30 ops sweep so
#                             its heartbeat is checkable like every other
#                             monitored job. Miners register CANDIDATEs
#                             only — nothing surfaces without the harness.
#  20. src.eod_summary:     Mon-Fri 15:45 IST. Daily MTM P&L + active
#                             positions + net delta card. Was DOCUMENTED at
#                             15:30 in its own docstring but was NEVER IN THIS
#                             SCRIPT and is called by no module — i.e. it has
#                             never fired in production. 15:45 not 15:30: the
#                             master_scheduler self-terminates AT 15:30, so a
#                             15:30 card can read the book mid-shutdown and
#                             miss a last-minute exit. 15:45 also clears main
#                             (15:35) and chain_archiver (15:40). Reads only
#                             journal.jsonl + brain_map.db (no Dhan calls), so
#                             it never contends for the token.
#
#  19. src.ceo_brief:       Mon-Fri 16:30 IST. The Daily CEO Brief — ONE
#                             cross-department card (operations / issues /
#                             deployments / risk) through notifier.fire_broadcast.
#                             16:30 NOT 16:00: main (15:35) and chain_archiver
#                             (15:40) are still inside their 30-min heartbeat
#                             grace at 16:00, so a 16:00 card could judge only
#                             3 of 13 jobs. Sits BEFORE the 18:50-20:30 evening
#                             block on purpose — those show as "not due yet"
#                             and the 20:30 ops sweep judges them. Read-only;
#                             keeps its OWN log-sweep offset so it never
#                             consumes ops_monitor's findings (#6).
#
#  30. src.analysis.dynamic_pricer + src.analysis.darling_tiers:
#                             Mon-Fri 19:18 / 19:22 IST (wired 2026-08-11).
#                             The VM now builds the equity desk's levels and
#                             tier table itself, off its own bhavcopy lake,
#                             instead of waiting for a Mac that may be
#                             asleep. valuation_scorer stays Mac-only —
#                             running it here empties the valuation file and
#                             greys out every darling. Details at the entry.
#
#  29. src.ingestion.cross_asset: Daily 19:40 IST (wired 2026-08-07). MCX
#                             commodities + global-index EOD bars into the
#                             lake. Capture-only — no Dept 2/3 importer, so
#                             it cannot touch a proposal or a size. Full
#                             reasoning at the entry itself, below.
#
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

# 25. Morning Brief (Directive 2, 2026-07-27, Mon-Fri 08:05 IST) — the
#     distinct pre-open account-level card: last night's macro read (via
#     ceo_language's two honesty gates — never names an analog unless the
#     regime engine declared it, never "executing"), today's watchlist/
#     book results-date proximity, and the book going into the session.
#     After renew_token (07:00) + suggest (08:00, the per-ticker technical
#     read this does NOT duplicate); before master_scheduler (09:10).
5 8 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.morning_brief >> "$REPO_ROOT/logs/morning_brief.log" 2>&1

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

# 8. EOD bulk & block deals footprint (Daily at 19:30 IST). NSE publishes
#    the large-deal report after close (~19:00); this pulls it, aggregates
#    the per-ticker net smart-money footprint, and writes data/bulk_deals.json.
#    Advisory-only (decision #60), fails open, runs before the 20:00 sleep phase.
# 28. NSE corporate announcements (Daily 19:25 IST, WIRED 2026-08-05).
#     Was a one-shot backfill only: data/lake/events/ held 2,601
#     partitions ending 2026-07-16 and NOTHING read them. Now it is
#     the evidence for equity_entry_checks.corporate_risk_halt, which
#     FAILS CLOSED on a stale feed — so this job existing is what
#     makes that halt a safety feature instead of theatre. Same NSE
#     cookie-handshake doctrine as deals_tracker (19:30) and its own
#     minute so the two never share the tape.
25 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.corporate_events >> "$REPO_ROOT/logs/corporate_events.log" 2>&1

30 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.deals_tracker >> "$REPO_ROOT/logs/deals_tracker.log" 2>&1

# 9. EOD option-chain capture (Mon-Fri 15:40 IST) — the one dataset that is
#    unbuyable later (decision #36: historical chains aren't retrievable).
#    Runs AFTER the 15:30 scheduler self-termination: zero token contention.
40 15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.chain_archiver >> "$REPO_ROOT/logs/chain_archiver.log" 2>&1

# 10. Perishable-artifact snapshots (Daily 19:45 IST) — news_sentiment.json
#     and the macro matrix are overwritten/never persisted; archive each
#     day's copy into the lake so cross-layer history accumulates.
45 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.daily_archiver >> "$REPO_ROOT/logs/daily_archiver.log" 2>&1

# 11. Earnings/results calendar (Daily 19:20 IST) — deterministic
#     days_to_results feed; whole-calendar overwrite so postponements heal.
20 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.earnings_calendar >> "$REPO_ROOT/logs/earnings_calendar.log" 2>&1

# 12. FII/DII daily cash flows (Daily 19:35 IST) — who IS moving the
#     indices; one row/day into data/ + the lake.
35 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.flows_tracker >> "$REPO_ROOT/logs/flows_tracker.log" 2>&1

# 16. News sentiment refresh (Daily 19:10 IST) — Google-News RSS -> Gemini
#     -> data/news_sentiment.json (the CLOUD LLM the VM can reach; NOT the
#     Ollama news_parser). Runs BEFORE daily_archiver (19:45) so the day's
#     news is fresh when it's snapshotted into the lake. Was never
#     scheduled — forecast.py consumed a stale/absent file. Gemini's 2k
#     budget covers a daily watchlist pass; fails open to "none".
10 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.news_processor >> "$REPO_ROOT/logs/news_processor.log" 2>&1

# 13. Weekly proving-harness digest (Saturday 10:00 IST) — what's in trial,
#     what validated/died this week, the placebo false-discovery rate.
#     Read-only over brain_map.db; the owner's window on the harness.
0 10 * * 6 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.validation.digest >> "$REPO_ROOT/logs/harness_digest.log" 2>&1

# 14. Portfolio-Greeks advisory (decision #71) — cron fires every 2h at
#     minute 30 (never the minute-0 report/renewal slots); the SCRIPT
#     self-gates on market hours and exits quietly otherwise. Aggregates
#     the open book's net delta/vega from the live chain Greeks and posts
#     ONE card/day only if a Vega or Delta budget breaches (OK = silent
#     snapshot). Advisory-only, fail-open. Kill switch in config.json.
30 */2 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.portfolio_greeks >> "$REPO_ROOT/logs/portfolio_greeks.log" 2>&1

# 15. Weekly track-record metrics (Saturday 10:05 IST, just after the
#     harness digest) — Sharpe/Sortino/max-drawdown over the REAL resolved
#     paper trades (decision #72). Posts only once there are enough trades
#     for an honest read; abstains silently below the floor. Read-only.
5 10 * * 6 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.performance >> "$REPO_ROOT/logs/performance.log" 2>&1

# 17. Official-RSS news pull (Daily 18:50 IST) — reads publishers' OWN RSS
#     feeds (config/rss_feeds.json; Moneycontrol/ET/BS), dedups, classifies
#     only NEW headlines via the Text Intelligence Manager (decision #75).
#     COST-SAFE by default: rss_backend inherits the global "ollama" backend,
#     so on the VM classification just skips (zero API spend) until the owner
#     enables the cloud backend after confirming API credits. RSS only — no
#     scraping, so no IP ban.
#     *** DISABLED 2026-08-05 (owner ruling, Sequence 2) ***
#     It provided no edge to M1. rss_backend inherits the global
#     "ollama" backend and the VM runs no Ollama by design (#47), so
#     classification was a permanent no-op: the job spent a nightly
#     NSE-free fetch to append UNCLASSIFIED headlines to
#     data/rss_signals.jsonl (183KB), which nothing reads. Capture
#     with no consumer and no classifier is not a pipeline.
#     Re-enable by uncommenting AND restoring rss_ingester.log to
#     ops_monitor.EXPECTED_JOBS (removed in the same commit — a
#     heartbeat for a job that no longer runs would flag SILENT
#     every night and, through discovery/nightly.health_gate, block
#     the miner forever).
# 50 18 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.rss_ingester >> "$REPO_ROOT/logs/rss_ingester.log" 2>&1

# 18. Gated nightly discovery pass (Daily 20:20 IST, decision #76) — the
#     Phase-5 miners behind three gates: ops heartbeats green + no INGESTION
#     problem lines in the latest sweep + daily_context >= 40 frames. A skip
#     is exit 0 + one log line; every 7th consecutive skip fires one Discord
#     note (the gate can never die silently). After sleep_phase (20:00),
#     before the 20:30 ops sweep (heartbeat convention). CANDIDATEs only —
#     the proving harness still owns every promotion.
20 20 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.discovery.nightly >> "$REPO_ROOT/logs/discovery_nightly.log" 2>&1

# 20. EOD summary card (Mon-Fri 15:45 IST) — today's MTM P&L, active
#     positions, net delta. Its docstring has claimed "15:30 IST" since it
#     was written, but the job was never installed here and nothing imports
#     it: the card has never actually fired. Scheduled at 15:45 so it reads
#     the book AFTER master_scheduler's 15:30 self-termination rather than
#     during it. Journal + brain_map only — no Dhan token needed.
45 15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.eod_summary >> "$REPO_ROOT/logs/eod_summary.log" 2>&1

# 19. Daily CEO Brief (Mon-Fri 16:30 IST) — the owner's one card: did
#     everything run, what broke (bucketed: dead ticker / margin / token /
#     Dhan API), what code is live on this box + what was built today, and
#     one line of open exposure + realized P&L (every number reused from
#     eod_summary, never recomputed). Weekdays only: on a weekend there is
#     no session to report and the evening jobs are the ops sweep's business.
30 16 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ceo_brief >> "$REPO_ROOT/logs/ceo_brief.log" 2>&1

# 21. Firm treasury rotation (Mon-Fri 19:56 IST, decision #83) — re-routes
#     the equity desk's budget AFTER the Mac's ~19:20 artifact ship (fresh
#     tier table) and BEFORE the next session. One atomic budget move in
#     brain_map.db; deadband/step-capped; one Discord card per rotation.
#     MOVED 19:50 -> 19:56 on 2026-08-04. It was the ONLY pair on this box
#     sharing an exact minute (with macro_nightly), and macro_nightly is the
#     documented OOM risk on the 1 GB e2-micro — memory contention, not API
#     contention, was the real hazard. macro_nightly runs ~170s, so 19:56
#     clears it with margin.
56 19 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.firm_treasury --rotate >> "$REPO_ROOT/logs/firm_treasury.log" 2>&1

# 22. Internal bug ledger (Daily 20:40 IST, #84 Directive 5) — folds the
#     20:30 ops sweep's problem lines + silent rejections/halts/vetoes
#     into logs/autonomous_bug_report.jsonl for the Thursday Protocol.
40 20 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.bug_ledger >> "$REPO_ROOT/logs/bug_ledger.log" 2>&1

# 23. Intraday 15-minute price snapshot (every 15 min, Mon-Fri 09:00-15:45) —
#     the read-only lake tap (data/lake/intraday_15m.jsonl); the module
#     self-gates to 09:15-15:30 IST so the edge slots exit quietly. Was
#     running on the VM but WAS MISSING FROM THIS SCRIPT — added here so the
#     source of truth is complete. NOTE: if a manual crontab line for
#     intraday_tracker exists on the VM, remove it so this isn't double-run.
#     Rate-safe: all its calls share the host-wide _throttle() gate (dhan
#     DH-905 fix), so it can never starve the live loop.
*/15 9-15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.intraday_tracker >> "$REPO_ROOT/logs/intraday_15m.log" 2>&1

# 26. Daily darling price tap (Mon-Fri 15:50 IST) — ONE close-of-session
#     price for EVERY darling in data/darling_ids.json (~105 names), not
#     just the funded book, into data/lake/darlings_daily.jsonl.
#     WHY: the 15-minute sweep covers the watchlist + the desk's OPEN
#     positions, so a darling we are merely WAITING on was never priced by
#     anything on the VM — entry logic could not see a name reach its
#     buying zone. That signal used to depend entirely on the Mac's 19:15
#     bhavcopy chain, which only fires when the Mac is awake (audited
#     2026-08-04: 4 of 11 recent weekdays missed, with no log line at all
#     on the missed days). The VM is always up. Complements bhavcopy
#     rather than replacing it: bhavcopy carries the whole exchange with
#     OHLC, this carries the darlings with one close.
#     15:50 keeps it clear of eod_summary (15:45) and the last intraday
#     slot; rate-safe via the host-wide _throttle().
50 15 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.intraday_tracker --darlings >> "$REPO_ROOT/logs/darlings_daily.log" 2>&1

# 27. Bhavcopy EOD fetch (daily 19:15 IST) — MIGRATED FROM THE MAC 2026-08-04.
#     The whole-exchange daily OHLC (~3,300 symbols/day) into
#     data/lake/bhavcopy/. It ran on the Mac inside patience_basket's 19:15
#     EOD chain, but macOS cron does not fire while the machine is asleep:
#     audited 2026-08-04, the Mac captured only 7 of 11 recent weekdays and
#     the missed days had NO log line at all (the job never ran, it did not
#     fail). The VM never sleeps. Verified from this box before wiring:
#     NSE serves the GCP IP (a real risk — NSE bot-blocks some scripted
#     access), and the first run recovered 2026-07-31, a day the Mac missed.
#     --backfill 5 is deliberate: fetch_day is idempotent ("already_have"),
#     so each run self-heals up to 5 days of holes instead of only today.
#     Source is NSE, NOT Dhan — no token, no rate-limit contention with any
#     market-data job. 19:15 sits 5 min clear of news_processor (19:10) and
#     earnings_calendar (19:20).
#     NOTE: the Mac's patience_basket chain still fetches its own copy for
#     tier grading; the two stores are per-machine and do not diverge.
15 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.bhavcopy_clerk --backfill 5 >> "$REPO_ROOT/logs/bhavcopy_clerk.log" 2>&1

# 30. Darling levels + tier grading ON THE VM (Mon-Fri 19:18 / 19:22 IST,
#     WIRED 2026-08-11). These two artifacts were Mac-only, produced by
#     the 19:15 `patience_basket --eod` chain and shipped down. The Mac
#     had not run since 08-05, so by 08-10 `darling_tiers.json` and
#     `darlings_levels.json` were 5.1 days stale — and the equity desk
#     FAILS CLOSED on a stale tier table (`_tiers_fresh`), so every extra
#     day of Mac sleep is a day the desk cannot enter a darling at all.
#     The VM can now do this itself: both stages derive from the bhavcopy
#     lake, which MIGRATED HERE on 08-04 and holds 100+ sessions.
#
#     ⚠️ ONLY THESE TWO STAGES. `valuation_scorer` is deliberately NOT
#     here: it needs the fundamentals/deep-read corpus that lives on the
#     Mac, and running it on this box scores 0 of 109 darlings and
#     overwrites `darlings_valuation.json` with an empty result — which
#     drives EVERY darling to `ungraded` on the next grading pass.
#     Measured on 2026-08-11 (caught by a --dry-run, repaired by
#     re-shipping the Mac's copy). `darlings_queue.json`,
#     `darlings_valuation.json` and `darling_pins.json` remain Mac-shipped
#     INPUTS; they change on a weekly screen cadence, not daily.
#
#     19:18 sits after this box's own bhavcopy (19:15, ~30s) so the levels
#     are built on TODAY's close — a 16:00 run would grade on yesterday's
#     data while making the file look fresh, which is worse than stale.
#     If the Mac is also awake its ship lands the same day's copy over
#     these; the tier file is its own de-dup ledger, so whichever runs
#     second simply sees no new transitions and fires no duplicate cards.
18 19 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.analysis.dynamic_pricer >> "$REPO_ROOT/logs/dynamic_pricer.log" 2>&1

22 19 * * 1-5 cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.analysis.darling_tiers >> "$REPO_ROOT/logs/darling_tiers.log" 2>&1

# 29. Cross-asset EOD tap (Daily 19:40 IST, WIRED 2026-08-07) — MCX
#     commodities + global indices into data/lake/cross_asset/. Built
#     2026-08-05 and never scheduled, so the only rows it ever wrote were
#     from one manual run. CAPTURE-ONLY: nothing in Dept 2/3 imports it,
#     so this cron cannot change a proposal, a gate or a size.
#     CONTRACT IDS ROT — that is this job's standing chore, not a bug.
#     An expired MCX future does not error, it just returns no bars, so
#     the job names the skip out loud in logs/cross_asset.jsonl and the
#     ids are rolled BY HAND against Dhan's scrip master (a guessed
#     successor id is ledger Issues 14/15 again). GOLD_INDIA sat dead
#     2026-08-05 → 08-13 and now runs on the OCT contract; CRUDE is next,
#     due 2026-08-19. The roll log lives in cross_asset.py's docstring.
#     READING THE LOG: CA-404 = the window was genuinely empty (holiday /
#     unentitled). CA-401 = the door REFUSED and quoted a DH-9xx — an
#     expired token, not a dead contract. Those were one message until
#     2026-08-13; do not conflate them again.
#     19:40 is the last free minute in the evening block: clear of
#     flows_tracker (19:35), daily_archiver (19:45) and the macro_nightly
#     (19:50) memory peak on the 1 GB e2-micro.
#     NOT in ops_monitor.EXPECTED_JOBS on purpose: a heartbeat there is
#     also a health_gate input, and a capture-only tap must not be able to
#     block the discovery miner. Wire it there once its ids are rolled.
40 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.ingestion.cross_asset >> "$REPO_ROOT/logs/cross_asset.log" 2>&1

# 24. Macro nightly heartbeat (Daily 19:50 IST) — the Dept-8 macro clock:
#     FRED globals ingest -> NSE indices ingest -> regime declare() onto the
#     immutable ledger -> Stage-B forward scoring (SB-2, fail-open last).
#     Was running on the VM but WAS MISSING FROM THIS SCRIPT — added so the
#     installer matches the VM's active crontab. NOTE: if a manual crontab
#     line for macro_nightly exists on the VM, remove it so this isn't
#     double-run. 19:50 shares the minute with firm_treasury (#21, Mon-Fri)
#     deliberately: neither touches the Dhan token, so no contention.
50 19 * * * cd "$REPO_ROOT" && "$PYTHON_BIN" -m src.analysis.macro_nightly >> "$REPO_ROOT/logs/macro_nightly.log" 2>&1
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
