# Adversarial audit — Chunk 5: Ingestion & Data Lake (2026-10-09)

Method: two finder lenses (data quality & staleness of values; failure modes, tokens, rate limits, cron hygiene) read the files whole; the lead confirmed or refuted each against the code, defaulting to "refuted" when uncertain. Fixes are queued for the next usage window (the 85% guardrail).

## Scope
`src/dhan_client.py`, `src/dhan_guard.py`, `src/token_provider.py`, `src/renew_token.py`, `src/data_fetcher.py`, `src/lake.py`, the candle sink in `src/live_bridge.py`, `src/ingestion/*.py`, `src/news_processor.py`, `src/nse_calendar.py`, `src/staleness_guard.py`, `src/ops_monitor.py`, the quote parsing in `src/execution/live_pricer.py`, `scripts/setup_cron.sh` and the scripts it calls.

## Findings

### Lens Q — data quality & staleness (8 raised; lead reproduced Q1, Q2, Q4)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| Q1 | **blocker** | CONFIRMED, **FIXED** | `fo_bhavcopy.py:172` ranks liquidity tiers by stock-option traded value; NSE's fo zip has carried FUT rows only since 07-14, so `opt_val` is 0 for all 213 symbols and tier1 was FILE ORDER. `liquidity_filter` refused the most liquid names; `liquidity_slippage` charged ICICIBANK/INFY the illiquid tier. | `data/fo_liquidity.json`: 0 symbols with opt_val>0; ICICIBANK rank 184 'illiquid', HDFCBANK 63. Fix: rank on `fut_val` when no option rows, `rank_basis` recorded. The next 19:xx bhavcopy run rebuilds the tiers. |
| Q2 | major | CONFIRMED, **FIXED** | `parse_secban` keeps `isalnum()` symbols only: M&M, GVT&D, BAJAJ-AUTO, NAM-INDIA can never read as banned. | `parse_secban('1,M&M\n2,BAJAJ-AUTO\n3,SAIL')` → `['SAIL']`. |
| Q3 | major | plausible | `deals_tracker.py:637` stamps live deals with the RUN date; the 19:30 cron runs daily incl. weekends → Friday's snapshot appended Fri/Sat/Sun, tripling `smart_money_trend` volume feeding the (now live, W1) bullish veto. | Lens scratch run: n_deals 3 for one deal. Verify on the VM: `grep -c '"as_of": "2026-10-04"' data/deals_history.jsonl` (a Sunday) should be 0. |
| Q4 | medium | CONFIRMED | `options_proposer._leg_fill` (`:446`): a quote >50% off LTP is not refused — the leg fills at the stale LTP (always the favourable side) with `fill_basis: "ltp"`; `live_pricer.crossed_open_price` abstains in the same case. | `_leg_fill(bid 12, ltp 30, SELL)` → `(30.0, 'ltp')`. Fix = refuse (abstain) like the live arm — entry-path change, dry-run first. |
| Q5 | medium | plausible | The only freshness check (`SafeDhanClient.freshness_error`) is not on any trading path; chain payloads carry no timestamp so it could never fire on a chain. The holiday calendar is the only defence against a frozen chain (Issue 41). | `grep -rn SafeDhanClient src` → wealth_lock, portfolio_report, portfolio_greeks, macro_tracker, cross_asset only. |
| Q6 | medium | plausible | `news_processor._clean_entry` turns a non-numeric/missing score into 0 with `stale: False` → a false "narrative collapsed" reversal card and a fake 0 ingested. | `_clean_entry({"short_term_catalyst_score":"bullish"})`. |
| Q7 | low | plausible | `flows_tracker.run` defaults `as_of` to today when the payload has no parseable date — a permanent lake row under the wrong session. | |
| Q8 | low | plausible | `live_pricer._node` returns `{}` for an ABSENT strike and the long leg is valued 0.0 with `ok=True` (a data hole becomes a mark). | `crossed_mark` with a missing 24100PE → ok True, 24100PE 0.0. |

Refuted by the lens: today's close not double-counted in SMA/RSI; IST host enforced by cron; exact strike keys; expiries from Dhan's list; VIX None kept as unknown; lake writes atomic/deduped; equity bhavcopy header strip OK.

### Lens F — failure modes, tokens, rate limits, cron hygiene (9 raised; lead verified F1 by grep + test)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| F1 | **major** | CONFIRMED, **FIXED** | `SafeDhanClient._call` (`dhan_guard.py:284-299`) called the SDK with no `_throttle()` and retried after a bare 1.1 s — the 2-hourly report/greeks jobs (10:30/12:30/14:30) burst the chain endpoint and never pushed the live loop's own chain calls back (DH-904 → "option chain unavailable", the Issue 40 class). Failures were in-memory only. | `grep -n _throttle src/dhan_guard.py` → nothing. Fix: reserve the host-wide slot (chain lane for chain endpoints) before each call and retry. |
| F2 | major | plausible | `renew_token.py:322-323`: one renewal attempt per day; a transport/5xx/Secret-Manager blip is never retried and nothing pages — the session runs blind until the 16:30 CEO brief. | Conflicts with the review-flags-to-Discord directive; #84 limits real-time pages to crashes → owner ruling (retry ×3 + a page is the obvious fix). |
| F3 | medium | plausible | `dhan_client._read_slot` clamps a slot > now+4.6 s as corrupt, so a deep queue (entry + exit threads + cron tracker + api poll) double-books slots. | Lens simulation: 5 chain callers reserved [0, 3.5, 7.0, 3.5, 7.0]. |
| F4 | medium | plausible | `ceo_brief.py:258` humanises DH-906 as a per-instrument data gap; `ops_monitor`/`dhan_guard` treat it as an invalid token (what the repo observed live). | Same class as Issue 26's DH-902 mislabel. |
| F5 | medium | plausible | Quote refusals (`_quote_sec`, `get_live_price_by_id`) swallow the DH code; "no market state this cycle" is not a problem line, so a mid-session auth death leaves no RED until the 20:30 card. | `ops_monitor.is_problem_line(...)` → False. |
| F6 | medium | plausible | No log rotation anywhere; disk only a telemetry number; on the e2-micro a full disk fails every job open at once with no RED first. | `grep -rni logrotate src scripts` → nothing. |
| F7 | medium-low | plausible | `macro_nightly` has no heartbeat entry; its artifact is flagged only after 72 h — each lost night costs the Stage-B ledger a session (#86 slack 0). | `EXPECTED_JOBS` lacks it. |
| F8 | low | plausible | "GEMINI_API_KEY not set — neutral (stale)" never counts as a problem; the feed can stay dead silently. | Consumers abstain correctly. |
| F9 | low | unconfirmed | The mirror push's ssh read-back has no overall timeout and the `*/15` job no lock. | |

**Time-sensitive (not a code defect):** HANDOVER 09-10 says the Dhan DATA plan is valid to **2026-10-10 (Saturday)**. If it lapses, Monday's signature is DH-902; with F5 the clearest signals are `cross_asset` CA-401 lines and the 08:00 `suggest.log`.

Refuted by the lens: mirror snapshots consistent; `_real_capture_for` and `live_pricer` chain fetches ARE on the throttle; recon GETs throttled; no retry double-writes; 15:30 self-termination does not race EOD; no secrets in logs; token self-heal works; DH-905 correctly classed.

**Status (2026-10-09 ~06:00 IST):** Q1, Q2, F1 FIXED and pushed. Q3–Q8, F2–F9 queued for the next usage window (85% guardrail); Q4 and Q3 first (entry-path effects → dry-run).
