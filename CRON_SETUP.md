# CRON_SETUP.md — Who Runs What, Where (post VM migration, 2026-07-08)

Since decision #47 the **VM is the trading engine** — the Mac is not
required for anything market-hours. This file documents both machines'
schedules and how to (re)install them.

## The VM (the engine — always on)

All six jobs are installed by the idempotent script (safe to re-run after
every `git pull`):

```bash
# on the VM (SSH from the Mac: gcloud compute ssh adigupta1998@alpha-trading-vm \
#   --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a)
cd ~/alpha_trading && bash scripts/setup_cron.sh
```

| IST | Job | Notes |
|---|---|---|
| 07:00 daily | `src.renew_token` | mints the day's Dhan token — V2 creds fetched from **GCP Secret Manager** at runtime (never on disk). ⚠️ **INTERIM (2026-07-10 hotfix, until the weekend deploy): this line is DISABLED (commented) on the VM; the only renewal is root's cron at 06:30/18:30 IST** — a 12:00 mint had blinded the live loop (ledger Issue 10). Deploy-day re-enables it and removes root's cron, per `docs/token_renewal_cadence.md`. |
| 08:00 Mon-Fri | `src.suggest` | daily suggestions digest |
| 09:10 Mon-Fri | `src.master_scheduler` | the full trading session; waits for 09:15, self-terminates 15:30 |
| 15:35 Mon-Fri | `src.main` | watchlist alert checks |
| 19:30 daily | `src.ingestion.deals_tracker` | EOD bulk & block deals footprint → `data/bulk_deals.json` — NSE publishes ~19:00, so this lands after it; advisory-only (decision #60), fails open |
| 20:00 daily | `src.sleep_phase` | Brain Map pass — decay-only on the VM (no Ollama there; edge mining happens from the Mac) |
| 20:30 daily | `src.ops_monitor` | log sweep + job heartbeats → Discord health card |

Also on the VM (systemd, `Restart=always`, enabled on boot): the API
gateway (`alpha-trading`), the Discord bot (`alpha-discord-bot`), and the
Cloudflare tunnel (`cloudflared-tunnel`). The old `alpha-market-loop`
service is **disabled — do not re-enable** (superseded by the scheduler).

Requirements baked into the VM already: `cloud-platform` OAuth scope
(needed for Secret Manager; changing scopes requires a stop/start) and
per-secret IAM grants for `dhan-pin` / `dhan-totp-secret` /
`dhan-api-key` / `dhan-api-secret`.

## The Mac (development + chat agent + opportunistic miner)

**LaunchAgent** (installed at `~/Library/LaunchAgents/com.adityagupta.alpha-edge-miner.plist`):
runs `scripts/mine_edges.sh` at every login and daily at 21:00. The miner
itself decides whether it's due (>20h since last success, Ollama up) and
skips silently otherwise — so the Mac being open more often costs nothing.
Reinstall if ever needed:

```bash
launchctl load ~/Library/LaunchAgents/com.adityagupta.alpha-edge-miner.plist
```

**Crontab (as of 2026-07-20): TWO Dept-8 jobs — the Darling two-clock
architecture (decision #77).** Both are Mac-only by the boundary doctrine
(the bhavcopy lake, the pricer and the valuation engine all live here; the
VM holds none of it) and both are NSE-crawling, which must never run from
the VM's IP:

| When | Job | What |
|---|---|---|
| 19:15 Mon–Fri | `src.analysis.patience_basket --eod` | THE DAILY CLOCK: today's bhavcopy → F&O bundle → pricer → valuation → 7-tier grading → darling shadow leg (Buy-tier entries, Strong-Sell forced exits). Log: `logs/patience_eod.log` |
| 10:00 Saturday | `src.analysis.weekly_recalibration` | THE WEEKLY CLOCK: refresh quarterly filings → re-screen fundamentals → No-Orphan pins → rebuild pricer/valuation/tiers → one summary card. Log: `logs/weekly_recalibration.log` |

Installed by the owner from their own Terminal (Mac cron install is
blocked for Claude by TCC — the edge-miner precedent). The crontab carries
`SHELL=/bin/bash` so cron inherits the Full Disk Access granted to bash
during the edge-miner fix, and both lines invoke python by ABSOLUTE path
(`/Library/Frameworks/Python.framework/Versions/3.14/bin/python3`) — the
standing lesson from being bitten three times in 48h. Re-install safely
(replaces rather than duplicates):

```bash
( crontab -l 2>/dev/null | grep -v -e 'src.analysis.patience_basket' -e 'src.analysis.weekly_recalibration' -e '^SHELL='; echo 'SHELL=/bin/bash'; echo '15 19 * * 1-5 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.analysis.patience_basket --eod >> logs/patience_eod.log 2>&1'; echo '0 10 * * 6 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.analysis.weekly_recalibration >> logs/weekly_recalibration.log 2>&1' ) | crontab -
```

**NEVER schedule on the Mac:** token renewal or push. The Mac used to run
its own 07:00 renewal + 07:10 push as "redundancy" — removed 2026-07-09
after discovering DhanHQ allows only one active token per account, so the
Mac's unattended renewal could invalidate and overwrite the VM's
currently-valid token (decision #48). The VM's Secret-Manager renewal
needs no backup. `scripts/push_token_to_vm.sh` still exists for manual
troubleshooting — just never schedule it again.

## Watching it work

```bash
# VM session log (live), from the Mac:
gcloud compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f \
  --zone=us-central1-a --command='tail -20 ~/alpha_trading/logs/master_scheduler.log'

# Mac miner log:
tail -20 logs/edge_miner.log
```

A healthy day: 🟢 session-open card at 09:15 and 🔴 close card at 15:30
(both from the VM), the 20:30 ops health card, and — whenever the Mac was
on that day — an edge-miner line in its log around login/21:00.
