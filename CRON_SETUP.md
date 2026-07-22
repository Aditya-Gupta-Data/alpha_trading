# CRON_SETUP.md — Who Runs What, Where

Ground truth for every schedule is `scripts/setup_cron.sh` (VM) and the Mac
crontab block below — this file is the human/LLM-readable mirror. If a count or
time here ever disagrees with `setup_cron.sh`, the script wins; fix this file.

Since decision #47 the **VM is the trading engine** — the Mac is not required
for anything market-hours. Token-renewal cadence + rationale: `docs/token_renewal_cadence.md`
and `DECISIONS.md` #48/#51.

## The VM (the engine — always on)

All 23 jobs are installed by the idempotent script (safe to re-run after every
`git pull`):

```bash
# on the VM (SSH from the Mac: gcloud compute ssh adigupta1998@alpha-trading-vm \
#   --project=project-37632031-10d0-47dd-b6f --zone=us-central1-a)
cd ~/alpha_trading && bash scripts/setup_cron.sh
```

Ordered by IST fire time (`CRON_TZ=Asia/Kolkata`; the script refuses to install
unless the host clock is +0530 — Debian cron ignores `CRON_TZ`, ledger Issue 1):

| IST | Job | What |
|---|---|---|
| 07:00 daily | `src.renew_token` | Mints the day's Dhan token — V2 creds fetched from **GCP Secret Manager** at runtime, never on disk. THE only scheduled renewal (never add a second — #48). |
| 08:00 Mon-Fri | `src.suggest` | Daily momentum/trend suggestions digest. |
| 09:10 Mon-Fri | `src.master_scheduler` | The full automated paper session; waits for the 09:15 open, self-terminates 15:30 (#45). |
| every 15m, mkt hrs Mon-Fri | `src.ingestion.intraday_tracker` | Read-only 15-min price snapshot → `data/lake/intraday_15m.jsonl` (not traded on yet). Self-gates to 09:15–15:30; fail-open per ticker. All calls share the host-wide `_throttle()` gate (DH-905 fix). |
| 15:35 Mon-Fri | `src.main` | Watchlist alert checks. |
| 15:40 Mon-Fri | `src.ingestion.chain_archiver` | EOD option-chain capture — unbuyable later (#36). After the 15:30 self-termination ⇒ zero token contention. |
| 15:45 Mon-Fri | `src.eod_summary` | MTM P&L + active positions + net-delta card. Journal + brain_map only (no Dhan token). |
| 16:30 Mon-Fri | `src.ceo_brief` | ONE cross-department card (ops / issues / deploys / risk / P&L). Reuses eod_summary's numbers. |
| 18:50 daily | `src.ingestion.rss_ingester` | Publishers' own RSS → dedup → classify NEW via Text Intelligence Manager (#75). Cost-safe: inherits the `ollama` backend ⇒ zero API spend on the VM until enabled. |
| 19:10 daily | `src.news_processor` | Google-News RSS → Gemini → `data/news_sentiment.json` (the cloud LLM the VM can reach). Feeds `forecast.py`. |
| 19:20 daily | `src.ingestion.earnings_calendar` | `days_to_results` feed; whole-calendar overwrite so postponements heal. |
| 19:30 daily | `src.ingestion.deals_tracker` | EOD bulk & block deals footprint → `data/bulk_deals.json`. Advisory-only (#60), fails open. |
| 19:35 daily | `src.ingestion.flows_tracker` | FII/DII daily cash flows; one row/day into `data/` + the lake. |
| 19:45 daily | `src.ingestion.daily_archiver` | Snapshots perishable news/macro artifacts into the lake before sleep_phase. |
| 19:50 Mon-Fri | `src.firm_treasury --rotate` | Re-routes the equity desk's budget after the Mac's ~19:20 artifact ship (#83). |
| 20:00 daily | `src.sleep_phase` | Brain Map pass — decay-only on the VM (no Ollama; edge mining runs opportunistically from the Mac). |
| 20:20 daily | `src.discovery.nightly` | Gated Phase-5 miner pass (#76): skips (exit 0) unless ops heartbeats green + no INGESTION problems + `daily_context` ≥ 60 frames. Every 7th skip fires one Discord note. |
| 20:30 daily | `src.ops_monitor` | Log sweep + job heartbeats → Discord health card. |
| 20:40 daily | `src.bug_ledger` | Folds the ops sweep's problem lines + silent rejections/halts into `logs/autonomous_bug_report.jsonl` for the Thursday Protocol (#84). |
| every 2h :00 | `src.portfolio_report` | Report card; the SCRIPT self-gates to market hours and exits quietly otherwise. Even-hour slots never touch the 07:00 renewal minute. |
| every 2h :30 | `src.portfolio_greeks` | Book-level net delta/vega budget advisory (#71); self-gates like the report. One card/day only on a breach. Kill switch in `config.json`. |
| Sat 10:00 | `src.validation.digest` | Weekly proving-harness digest — what's in trial, what validated/died, the placebo false-discovery rate. Read-only. |
| Sat 10:05 | `src.performance` | Weekly Sharpe/Sortino/max-drawdown over the REAL resolved paper trades (#72); abstains silently below the floor. |

Also on the VM (systemd, `Restart=always`, enabled on boot): the API gateway
(`alpha-trading`), the Discord bot (`alpha-discord-bot`), and the Cloudflare
tunnel (`cloudflared-tunnel`). The old `alpha-market-loop` service is
**disabled — do not re-enable** (superseded by the scheduler).

Requirements baked into the VM already: `cloud-platform` OAuth scope (needed for
Secret Manager; changing scopes requires a stop/start) and per-secret IAM grants
for `dhan-pin` / `dhan-totp-secret` / `dhan-api-key` / `dhan-api-secret`.

`src.evolution` is deliberately NOT on the VM (it needs a local Ollama) — it runs
on the Mac via launchd (`scripts/com.alphatrading.evolution.plist`).

## The Mac (development + chat agent + opportunistic miner)

**LaunchAgent** (`~/Library/LaunchAgents/com.adityagupta.alpha-edge-miner.plist`):
runs `scripts/mine_edges.sh` at every login and daily at 21:00. The miner itself
decides whether it's due (>20h since last success, Ollama up) and skips silently
otherwise — so the Mac being open more often costs nothing. Reinstall if needed:

```bash
launchctl load ~/Library/LaunchAgents/com.adityagupta.alpha-edge-miner.plist
```

**Crontab (as of 2026-07-20): TWO Dept-8 jobs — the Darling two-clock
architecture (decision #77)**, plus the weekly scrip reconciliation. All Mac-only
by the boundary doctrine (the bhavcopy lake, pricer and valuation engine live
here; the VM holds none of it) and NSE-crawling, which must never run from the
VM's IP:

| When | Job | What |
|---|---|---|
| 19:15 Mon–Fri | `src.analysis.patience_basket --eod` | THE DAILY CLOCK: today's bhavcopy → F&O bundle → pricer → valuation → 7-tier grading → darling shadow leg (Buy-tier entries, Strong-Sell forced exits). Log: `logs/patience_eod.log` |
| 10:00 Saturday | `src.analysis.weekly_recalibration` | THE WEEKLY CLOCK: refresh quarterly filings → re-screen fundamentals → No-Orphan pins → rebuild pricer/valuation/tiers → one summary card. Log: `logs/weekly_recalibration.log` |
| 09:30 Saturday | `src.ingestion.scrip_master` | SCRIP RECONCILIATION: diffs every `SECURITY_ID_MAP` id against Dhan's public scrip master; de-duped review card on any mismatch. Log: `logs/scrip_master.log` |

Installed by the owner from their own Terminal (Mac cron install is blocked for
Claude by TCC — the edge-miner precedent). The crontab carries `SHELL=/bin/bash`
so cron inherits the Full Disk Access granted to bash during the edge-miner fix,
and both lines invoke python by ABSOLUTE path
(`/Library/Frameworks/Python.framework/Versions/3.14/bin/python3`). Re-install
safely (replaces rather than duplicates):

```bash
( crontab -l 2>/dev/null | grep -v -e 'src.analysis.patience_basket' -e 'src.analysis.weekly_recalibration' -e 'src.ingestion.scrip_master' -e '^SHELL='; echo 'SHELL=/bin/bash'; echo '15 19 * * 1-5 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.analysis.patience_basket --eod >> logs/patience_eod.log 2>&1'; echo '30 9 * * 6 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.ingestion.scrip_master >> logs/scrip_master.log 2>&1'; echo '0 10 * * 6 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m src.analysis.weekly_recalibration >> logs/weekly_recalibration.log 2>&1' ) | crontab -
```

**NEVER schedule on the Mac:** token renewal or push. The Mac used to run its own
07:00 renewal + 07:10 push as "redundancy" — removed 2026-07-09 after discovering
DhanHQ allows only one active token per account, so the Mac's unattended renewal
could invalidate and overwrite the VM's currently-valid token (decision #48). The
VM's Secret-Manager renewal needs no backup. `scripts/push_token_to_vm.sh` still
exists for manual troubleshooting — just never schedule it again.

## Watching it work

```bash
# VM session log (live), from the Mac:
gcloud compute ssh adigupta1998@alpha-trading-vm --project=project-37632031-10d0-47dd-b6f \
  --zone=us-central1-a --command='tail -20 ~/alpha_trading/logs/master_scheduler.log'

# Mac miner log:
tail -20 logs/edge_miner.log
```

A healthy day: 🟢 session-open card at 09:15 and 🔴 close card at 15:30 (both from
the VM), the 20:30 ops health card, and — whenever the Mac was on that day — an
edge-miner line in its log around login/21:00.
